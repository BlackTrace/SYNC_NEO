#! /usr/bin/env python3
# coding: utf-8
# flow@蓝鲸淘
# Licensed under the MIT License.

import os
import sys
import time
import uvloop
import asyncio
import aiohttp
import datetime
import motor.motor_asyncio
from logzero import logger
from decimal import Decimal as D
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


now = lambda:time.time()

def get_mongo_uri():
    mongo_server = os.environ.get('MONGOSERVER')
    mongo_port   = os.environ.get('MONGOPORT')
    mongo_user   = os.environ.get('MONGOUSER')
    mongo_pass   = os.environ.get('MONGOPASS')
    if mongo_user and mongo_pass:
        return 'mongodb://%s:%s@%s:%s' % (mongo_user, mongo_pass, mongo_server, mongo_port)
    else:
        return 'mongodb://%s:%s' % (mongo_server, mongo_port)

def get_neo_uri():
    neo_node = os.environ.get('NEONODE')
    neo_port = os.environ.get('NEOPORT')
    return 'http://%s:%s' % (neo_node, neo_port)

get_mongo_db = lambda:os.environ.get('MONGODB')

get_tasks = lambda:os.environ.get('TASKS')

def sci_to_str(sciStr):
    '''科学计数法转换成字符串'''
    assert type('str')==type(sciStr),'invalid format'
    if 'E' not in sciStr:
        return sciStr
    s = '%.8f' % float(sciStr)
    while '0' == s[-1] and '.' in s:
        s = s[:-1]
    if '.' == s[-1]:
        s = s[:-1]
    return s

class Crawler:
    def __init__(self, mongo_uri, mongo_db, neo_uri, loop, tasks='1000'):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.state  = self.client[mongo_db].state
        self.history = self.client[mongo_db].history
        self.max_tasks = int(tasks)
        self.neo_uri = neo_uri
        self.processing = []
        self.cache = {}
        self.cache_utxo = {}
        self.session = aiohttp.ClientSession(loop=loop)

    async def get_block(self, height):
        async with self.session.post(self.neo_uri,
                json={'jsonrpc':'2.0','method':'getblock','params':[height,1],'id':1}) as resp:
            if 200 != resp.status:
                logger.error('Unable to fetch block {}'.format(height))
                sys.exit(1)
            j = await resp.json()
            return j['result']

    async def get_transaction(self, txid):
        async with self.session.post(self.neo_uri,
                json={'jsonrpc':'2.0','method':'getrawtransaction','params':[txid,1],'id':1}) as resp:
            if 200 != resp.status:
                logger.error('Unable to fetch transaction {}'.format(txid))
                sys.exit(1)
            j = await resp.json()
            return j['result']

    async def get_block_count(self):
        async with self.session.post(self.neo_uri,
                json={'jsonrpc':'2.0','method':'getblockcount','params':[],'id':1}) as resp:
            if 200 != resp.status:
                logger.error('Unable to fetch blockcount')
                sys.exit(1)
            j = await resp.json()
            return j['result']

    async def get_history_state(self):
        result = await self.state.find_one({'_id':'history'})
        if not result:
            await self.state.insert_one({'_id':'history','value':-1})
            return -1
        else:
            return result['value']

    async def update_history_state(self, height):
        await self.state.update_one({'_id':'history'}, {'$set': {'value':height}}, upsert=True)

    async def cache_block(self, height):
        self.cache[height] = await self.get_block(height)

    async def cache_utxo_vouts(self, txid):
        tx = await self.get_transaction(txid)
        self.cache_utxo[txid] = tx['vout']

    async def update_a_vin(self, vin, txid, index, utc_time):
        _id = txid + '_in_' + str(index)
        try:
            await self.history.update_one({'_id':_id},
                    {'$set':{
                        'txid':txid,
                        'time':utc_time,
                        'address':vin['address'],
                        'asset':vin['asset'],
                        'value':vin['value'],
                        'operation':'out'
                        }},upsert=True)
        except Exception as e:
            logger.error('Unable to update a vin %s:%s' % (_id,e))
            sys.exit(1)

    async def update_a_vout(self, vout, txid, index, utc_time):
        _id = txid + '_out_' + str(index)
        try:
            await self.history.update_one({'_id':_id},
                    {'$set':{
                        'txid':txid,
                        'time':utc_time,
                        'address':vout['address'],
                        'asset':vout['asset'],
                        'value':vout['value'],
                        'operation':'in'
                        }},upsert=True)
        except Exception as e:
            logger.error('Unable to update a vout %s:%s' % (_id,e))
            sys.exit(1)

    def timestamp_to_utc(self, timestamp):
        return datetime.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    async def crawl(self):
        self.start = await self.get_history_state()
        self.start += 1
        
        while True:
            current_height = await self.get_block_count()
            time_a = now()
            if self.start < current_height:
                stop = self.start + self.max_tasks
                if stop >= current_height:
                    stop = current_height
                self.processing.extend([i for i in range(self.start,stop)])
                max_height = max(self.processing)
                min_height = self.processing[0]
                await asyncio.wait([self.cache_block(h) for h in self.processing])
                if self.processing != sorted(self.cache.keys()):
                    msg = 'cache != processing'
                    logger.error(msg)
                    sys.exit(1)
                txids = [] 
                for block in self.cache.values():
                    for tx in block['tx']:
                        for vin in tx['vin']:
                            txids.append(vin['txid'])
                txids = list(set(txids))
                if txids:
                    await asyncio.wait([self.cache_utxo_vouts(txid) for txid in txids])
                if sorted(txids) != sorted(self.cache_utxo.keys()):
                    msg = 'cache utxo error'
                    logger.error(msg)
                    sys.exit(1)
                vins= []
                vouts = []
                for block in self.cache.values():
                    block_time = self.timestamp_to_utc(block['time'])
                    for tx in block['tx']:
                        utxo_dict = {}
                        for vin in tx['vin']:
                            utxo = self.cache_utxo[vin['txid']][vin['vout']]
                            key = utxo['asset'] + '_' + utxo['address']
                            if key in utxo_dict.keys():
                                utxo_dict[key]['value'] = sci_to_str(str(D(utxo_dict[key]['value'])+D(utxo['value'])))
                            else:
                                utxo_dict[key] = utxo
                        utxos = list(utxo_dict.values())
                        for i in range(len(utxos)):
                            utxo = utxos[i]
                            for j in range(len(tx['vout'])):
                                vout = tx['vout'][j]
                                if vout['asset']==utxo['asset'] and vout['address']==utxo['address']:
                                    utxo['value'] = sci_to_str(str(D(utxo['value'])-D(vout['value'])))
                                    del tx['vout'][j]
                                    break
                            vins.append([utxo, tx['txid'], i, block_time])
                        for k in range(len(tx['vout'])):
                            vout = tx['vout'][k]
                            vouts.append([vout, tx['txid'], k, block_time])
                if vins:
                    await asyncio.wait([self.update_a_vin(*vin) for vin in vins])
                if vouts:
                    await asyncio.wait([self.update_a_vout(*vout) for vout in vouts])

                time_b = now()
                logger.info('reached %s ,cost %.6fs to sync %s blocks ,total cost: %.6fs' % 
                        (max_height, time_b-time_a, stop-self.start, time_b-START_TIME))
                await self.update_history_state(max_height)
                self.start = max_height + 1
                del self.processing
                del self.cache
                del self.cache_utxo
                self.processing = []
                self.cache = {}
                self.cache_utxo = {}
            else:
               await asyncio.sleep(0.5)


if __name__ == "__main__":
    START_TIME = now()
    logger.info('STARTING...')
    mongo_uri = get_mongo_uri()
    neo_uri = get_neo_uri()
    mongo_db = get_mongo_db()
    tasks = get_tasks()
    loop = asyncio.get_event_loop()
    crawler = Crawler(mongo_uri, mongo_db, neo_uri, loop, tasks)
    try:
        loop.run_until_complete(crawler.crawl())
    except Exception as e:
        logger.error('LOOP EXCEPTION: %s' % e)
    finally:
        loop.close()
