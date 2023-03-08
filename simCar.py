import datetime,time
import sys
import json
import random
from config import *
import redis
r = redis.Redis(host='localhost', port=16379, db=1)

addr = "C001"

while True:    
    try:
        lsA = [False,False,False,False,False,False,False,False]
        lsB = [True,]
        lsC = [False,False,False,False,False,False,False,False,False,False]
        lsD = [True]
        pedActuate = random.choice(lsA)  #判斷有無行人觸動
        carActuate = random.choice(lsB)  #判斷有無車輛觸動
        pedOnRoad = random.choice(lsC)   #判斷有無行人在路中
        carOnRoad = random.choice(lsD)   #判斷有無車輛在路口
        
        if addr == "D002":
            addr = "D001"
        else:
            addr = "D002"

        nowTimeString = datetime.datetime.strftime(datetime.datetime.now(), '%Y-%m-%d %H:%M:%S')
        msg = json.dumps({"now": nowTimeString,"addr":addr,"pedActuate" :pedActuate ,"carActuate" : carActuate , "carOnRoad" : carOnRoad, "pedOnRoad" : pedOnRoad})
        print(msg)
        r.publish('receAi',msg)
        time.sleep(1)

    except:
        sys.exit(0)
