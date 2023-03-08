# -*- coding: utf-8 -*-
import os
import sys
import threading
import queue
import datetime
import time
import timeit
import simComdLib
import logging
import redis
import copy
import json
import requests
import re
import serial
from config import *
# 路口時制計畫參數
from timetable import *
import math
import subprocess
import stateClass

# 設定啟動全域參數
gblClearActuateStatus = False  # 是否已經清空觸動數值
gblSetGreenBranch = False  # 是否已經重設支道綠燈秒數

CycleDetectStats = {"cyPedTotal":0,"cyCarTotal":0,
                    "detectRec":{}                    
                    }
r = redis.Redis(host='localhost', port=16379, db=1)
subTc2State = r.pubsub()
subTc2State.subscribe('queTc2State')
subReceAi = r.pubsub()
subReceAi.subscribe('receAi')
queState2Center = queue.Queue()

# 設定log
if debug is True:
    log_filename = datetime.datetime.now().strftime("stateTC%Y-%m-%d_%H_%M_%S.log")
    logging.basicConfig(level=logging.DEBUG,
    format='%(asctime)s %(message)s', datefmt='%m-%d %H:%M:%S',filename=log_filename)    
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s: %(message)s', datefmt='%m-%d %H:%M:%S')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

else:
    logging.basicConfig(level=logging.DEBUG,format='%(asctime)s %(message)s', datefmt='%m-%d %H:%M:%S')
    logging.getLogger('chardet.charsetprober').setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)

# def clearQueue(q):
#     '''清除的Queue的舊資料
#     '''
#     queueEmpty = False
#     remaining = 20  # 最多等待20秒清空Queue
#     nowTime = datetime.datetime.now()
#     while (queueEmpty is False) and (remaining >= 0):
#         delaySecond = (datetime.datetime.now() - nowTime).total_seconds()
#         remaining = 20 - delaySecond
#         job = "no"
#         try:
#             job = q.reserve(timeout=0.01)
#             q.delete(job)
#         except:
#             pass
#         if job == "no":
#             queueEmpty = True

# # 清空歷史殘留的Queue，以避免被之前觸動紀錄誤動作
# clearQueue(receAiQueue)
# clearQueue(queTc2State)


# 初始化相關設施的物件參數
tc = stateClass.TrafficController("武嶺橋")
aiDevice = stateClass.AiDevice("Daxi")
tyCenter = stateClass.Center("Taoyuan")

for addr in RsDeviceNameList:
    aiDevice.setAiBackTime(addr,datetime.datetime.now())

def autoSendAPI(event):
    '''
    主動回報績效數據資訊
    '''
    url = "http://172.16.0.30:9421/v1/api/i-signal-ctrl/semi-actuated-signal-control"
    headers = {'Content-Type': 'application/json'}
    dataTemple ='''{{"activeId":"{activeId}","name":"半觸動路口號誌","desc":"武陵橋東側","datas":[{{"id":"triggerType","val":{triggerType}}},{{"id":"trigger-by-car","val":{triggerCar}}},{{"id":"trigger-by-person","val":{triggerPerson}}},{{"id":"without-trigger","val":{withoutTriger}}},{{"id":"phase-num","val":{phaseNum}}},{{"id":"phase1","val":{phase1}}},{{"id":"pedgreenflash1","val":{pedgreenflash1}}},{{"id":"pedred1","val":{pedred1}}},{{"id":"phase2","val":{phase2}}},{{"id":"pedgreenflash2","val":{pedgreenflash2}}},{{"id":"pedred2","val":{pedred2}}},{{"id":"phase3","val":{phase3}}},{{"id":"pedgreenflash3","val":{pedgreenflash3}}},{{"id":"pedred3","val":{pedred3}}},{{"id":"change-green-light","val":{addGreenSecond}}},{{"id":"start-with","val":{startTime}}},{{"id":"end-with","val":{endTime}}}],"active":true}}'''
    while not event.isSet():
        event.wait(1)
        try:
            data = queState2Center.get()
            queState2Center.task_done()
            dataResult = ""
            dataResult += dataTemple.format(**data)
            if debug is False:
                response = requests.post(url=url, headers=headers, data=dataResult.encode('utf-8'),timeout=2)
                logging.info(dataResult)
                logging.info(f'上傳結果={response.status_code}') 
            else:
                logging.info(dataResult)
        except Exception as e:
            logging.debug("error")
            logging.debug(e)

def timeCountDown(baseTime, timeout=60):
    countDownResult = False
    if  datetime.datetime.now() >= baseTime + datetime.timedelta(seconds=timeout) :
        countDownResult = True
    return countDownResult

def centerStartControl(event):
    '''
    接收中心的半觸動控制指令
    '''
    url = "http://172.16.0.30:9421/v1/api/toggle-switch-ctrl/sas-status/"
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    setStartTime  = datetime.datetime.now()
    while not event.isSet():
        event.wait(1)
        status = tyCenter.getCenterControlReport()
        if timeCountDown(setStartTime,30):
            setStartTime = datetime.datetime.now()
            msg={"status":status,"timestamp":int(datetime.datetime.now().timestamp())}
            try:
                if debug is False:
                    response = requests.post(url=url, headers=headers, data=json.dumps(msg),timeout=2)
                    logging.info(f'上傳結果={response.status_code}') 
                    msg = json.loads(response.text.encode('utf-8'))
                    logging.info(msg)
                    if msg["result"] is False:
                        tyCenter.setCenterControl(False)
                    else:
                        tyCenter.setCenterControl(True)
            except Exception as e:
                logging.debug("Error:上傳中心系統狀錯誤")
                logging.debug(e)

def sendAck(cmdSeq):
    '''
    傳送ACK給號誌控制器
    '''
    command = simComdLib.commandAck(cmdSeq)
    msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
    try:
        r.publish('queState2Tc',msg)
    except Exception as e:
        logging.debug("Error:無法分派Ack訊息")
        logging.debug(e)

def receTcEvent(event):
    '''
    處理控制器主動回報的相關指令
    5F+03 回報目前步階、秒數
    5F+0C 現行步階
    5F+C8 取得目前執行時制計畫編號及內容
    0F+C2 取得控制器時間
    5F+08 現場操作狀態
    5F+C0 回報目前控制策略
    5F+CC 回報目前控制策略與倒數秒數
    5F+EF 回報目前倒數秒數週期
    '''
    back5F03Time =datetime.datetime.now()    
    for job in subTc2State.listen():
        try:
            timer_start2 = time.time()
            if job['type'] == "message":
                msg = json.loads(job['data'])
            else:
                continue
            command = bytes.fromhex(msg["data"])
            receTime = datetime.datetime.fromtimestamp(msg["time"])
            receTimeString = datetime.datetime.strftime(receTime, '%Y-%m-%d %H:%M:%S')
            logging.info(f'{receTimeString}:{msg["data"]}')
            if command[7] == 95 and command[8] == 3:        # 5F+03的資訊                 
                timer_start = time.time()
                stepId = str(command[12]) + "-" + str(command[13])
                second = int.from_bytes(command[14:16], 'big')
                timer_end = time.time()
                logging.info(f'timeit receTcEvent2-1:{timer_end-timer_start}')                
                timer_start = time.time()
                ackSeq = command[2]
                sendAck(ackSeq)
                timer_end = time.time()
                logging.info(f'timeit receTcEvent2-2:{timer_end-timer_start}')                
                timer_start = time.time()
                tc.setStepParameter(stepId,second,datetime.datetime.now())
                timer_end = time.time()
                logging.info(f'timeit receTcEvent2-3:{timer_end-timer_start}')                
                timer_start = time.time()
                back5F03Time = datetime.datetime.now()
                logging.info(f'步階:{stepId}:{second}秒')
                timer_end = time.time()
                logging.info(f'timeit receTcEvent2-4:{timer_end-timer_start}')
            elif command[7] == 95 and command[8] == 12:     # 5F+0C 控制器主動回報目前步階
                timer_start = time.time()
                strategy = command[9]
                subPhaseId = str(command[10])
                stepId = str(command[11])
                tc.setControlStrategy(strategy)
                ackSeq = command[2]
                sendAck(ackSeq)
                timer_end = time.time()
                logging.info(f'timeit receTcEvent3:{timer_end-timer_start}')
            elif command[7] == 95 and command[8] == 200:    # 5F+C8 取得目前執行時制計畫編號及內容
                timer_start = time.time()
                planId = str(command[9])
                ackSeq = command[2]
                sendAck(ackSeq)
                logging.info(f'取得目前時制計畫編號={planId}')
                if tc.getPlanId() != planId:
                    if tc.getPlanId() == "-1":
                        tc.setPlanId(str(planId))
                        tc.setTimeTable(copy.deepcopy(TimeTableSecond[str(planId)]))
                    else:
                        tc.setPlanId(str(planId))
                        tc.setTimeTable(copy.deepcopy(TimeTableSecond[str(planId)]))
                        logging.info("時制轉換中.....感應性號誌系統暫停運作")
                        tc.setPlanChange(True)
                        tc.setPlanChangeStartTime(datetime.datetime.now())
                    tc.setDirect(command[10])
                    tc.setPhaseOrder(command[11])
                    subPhaseCount = command[12]
                    tc.setSubPhaseCount(subPhaseCount)
                    greenList = []                    
                    for i in range(subPhaseCount):
                        greenList.append(int.from_bytes(command[13+i*2:13+i*2+2], byteorder='big'))
                    #AABB01FFFF001C5FC803008104004B000A000F00230096003CAACCBC
                    tc.setGreenList(greenList)
                    tc.setCycleTime(int.from_bytes(command[13+subPhaseCount*2:13+subPhaseCount*2+2], byteorder='big'))
                    tc.setOffset(int.from_bytes(command[13+subPhaseCount*2+2:13+subPhaseCount*2+4], byteorder='big'))
                    timer_end = time.time()
                    logging.info(f'timeit receTcEvent4:{timer_end-timer_start}')
            elif command[7] == 15 and command[8] == 194:    # 0F+C2 回報控制器時間
                timer_start = time.time()
                year = 1911 + command[9]
                month = command[10]
                day = command[11]
                hour = command[13]
                minute = command[14]
                second = command[15]
                ackSeq = command[2]
                sendAck(ackSeq)                
                tcTime = datetime.datetime.now
                try:
                    tcTime = datetime.datetime(year, month, day, hour, minute, second, 0)                    
                    tc.tcLagTime = (datetime.datetime.now() - tcTime).total_seconds()
                    if os.name != "nt" and tc.tcLagTime < 10:  #控制器時間不應差距太大
                        # os.system(f'sudo date -s "{year}/{month}/{day} {hour}:{minute}:{second}" "+%Y/%m/%d %H:%M:%S"')
                        app = f'sudo date -s "{year}/{month}/{day} {hour}:{minute}:{second}" "+%Y/%m/%d %H:%M:%S"'
                        pid = subprocess.Popen([app], shell=True,stdin=None, stdout=None, stderr=None, close_fds=True)
                    logging.info(f'更新系統時間:{tc.tcLagTime}秒')
                    timer_end = time.time()
                    logging.info(f'timeit receTcEvent5:{timer_end-timer_start}')
                except Exception as e:
                    logging.debug("Error:對時失敗")
                    logging.debug(e)                    
            elif command[7] == 95 and command[8] == 8:      # 5F+08 現場操作回報
                timer_start = time.time()
                fieldOperate = command[9]
                ackSeq = command[2]
                sendAck(ackSeq)                                
                if fieldOperate in (1, 2, 64):
                    tc.setPoliceStatus(True)
                    tc.setPoliceRestore(False)
                    logging.info("執行策略改變,暫停感應性號誌 ")
                else:                    
                    tc.setPoliceRestore(True)
                    logging.info("執行策略改變,重設感應性號誌 ")
                timer_end = time.time()    
                logging.info(f'timeit receTcEvent6:{timer_end-timer_start}')    
            elif command[7] == 95 and command[8] == 192:    # 5F+C0 回報目前控制策略
                timer_start = time.time()
                tc.setControlStrategy(command[9])
                ackSeq = command[2]
                sendAck(ackSeq)              
                timer_end = time.time()     
                logging.info(f'timeit receTcEvent7:{timer_end-timer_start}')             
            elif command[7] == 95 and command[8] == 204:    # 5F+CC 回報目前控制策略及秒數
                timer_start = time.time()
                ackSeq = command[2]
                sendAck(ackSeq) 
                stepId = str(command[10]) + "-" + str(command[11])
                second = int.from_bytes(command[12:14], 'big')
                if (tc.getPlanId() in ("26","35")):   #閃黃時，良基的閃黃時制回報數據有問題
                    continue
                if (stepId == "0-0") or (second == 0) : #良基的5FCC有時會出現全零的狀況
                    continue
                tc.setControlStrategy(command[9])
                tc.setStepParameter(stepId,second,datetime.datetime.now())
                logging.info(f'步階:{stepId}:{second}秒c')
                timer_end = time.time()
                logging.info(f'timeit receTcEvent8:{timer_end-timer_start}')
            elif command[7] == 95 and command[8] == 239:    # 5F+EF 回報目前倒數秒數週期
                timer_start = time.time()
                transmitType = command[9]
                transmitCycle = command[10]
                ackSeq = command[2]
                sendAck(ackSeq)
                if (transmitType != 2) or  (transmitCycle != 1):
                    command = simComdLib.command5F3F(tc.getSeq(),2,1)
                    msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
                    try:
                        r.publish('queState2Tc',msg)
                    except Exception as e:                        
                        logging.debug("Error:查詢回報週期")
                        logging.debug(e)
                timer_end = time.time()
                logging.info(f'timeit receTcEvent9:{timer_end-timer_start}')
            elif command[7] == 15 and command[8] == 128:    # 0F+80 設定指令執行成功
                timer_start = time.time()
                ackSeq = command[2]
                confirmComd = "%X" %command[9] + "%X" %command[10]
                tc.setCycleChange(confirmComd,ackSeq,1,0)
                timer_end = time.time()
                logging.info(f'timeit receTcEventA:{timer_end-timer_start}')
            elif command[7] == 15 and command[8] == 129:     # 0F+81 設定指令執行失敗
                timer_start = time.time()
                ackSeq = command[2]
                confirmComd = "%X" %command[9] + "%X" %command[10]
                tc.setCycleChange(confirmComd,ackSeq,-1,0)
                timer_end = time.time()
                logging.info(f'timeit receTcEventB:{timer_end-timer_start}')
            timer_end2 = time.time()
            logging.info(f'timeit receTcEvent1:{timer_end2-timer_start2}')
        except Exception as e:            
            logging.debug("Error:receTcEvent")
            logging.debug(e)

def receAiEvent(event):
    '''
    接收Ai觸動訊息
    '''
    for job in subReceAi.listen():
        try:
            if job['type'] == "message":
                command = json.loads(job['data'])
            else:
                continue
            #由AI取得的時間
            aiTime = datetime.datetime.strptime(command["now"], '%Y-%m-%d %H:%M:%S')
            addr = command["addr"]
            #設定為當前的時間
            aiDevice.setAiBackTime(addr,datetime.datetime.now())

            if aiDevice.getAiBackTime(addr) < aiTime:  
                aiDevice.setAiBackTime(addr, aiTime)
            #避免過期的觸動影響判斷
            if aiTime + datetime.timedelta(seconds=30) < datetime.datetime.now():                    
                continue

            checkActuate = False
            PedestrianNum = 0
            PedestrianOnRoadNum = 0
            CarNum = 0
            CarOnRoadNum = 0

            if command["pedActuate"] is True:
                PedestrianNum = 1
                checkActuate = True                
            if command["carActuate"] is True:
                CarNum = 1
                checkActuate = True                
            if command["pedOnRoad"] is True:
                PedestrianOnRoadNum = 1
                checkActuate = True                
            if command["carOnRoad"] is True:
                CarOnRoadNum = 1
                checkActuate = True
            if checkActuate is True:
                tc.setActuate(aiTime,PedestrianNum,PedestrianOnRoadNum,CarNum,CarOnRoadNum)
        except Exception as e:
            logging.debug("Error: Ai")
            logging.debug(e)

def currentStepSecondChange(seq, subPhase, stepId, targetSecond):
    '''調整當前步階時間
    '''
    stepSecond = tc.getTimeTable()[f'{subPhase}-{stepId}']
    addSecond = targetSecond - stepSecond
    stepAddSecond = tc.getTimeTable()[f'{subPhase}-{stepId}'] + addSecond
    # stepAddSecond = TimeTableSecond[tc.getPlanId()][f'{subPhase}-{stepId}'] + addSecond
    command = simComdLib.command5F1C(seq, subPhase, stepId, stepAddSecond)
    msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
    logging.info(f'設定秒數:{subPhase}-{stepId}，由{stepSecond}秒，增加{addSecond}秒，到達{targetSecond}')
    try:
        r.publish('queState2Tc',msg)
        tc.setCycleChange("5F1C",seq,1,addSecond)
    except Exception as e:
        logging.debug("Error:無法分派訊息至sendCmdQueue")
        logging.debug(e)        

def planStepSecondChange(seq, subPhase, stepId, targetSecond):
    '''調整未來步階時間
    '''
    stepSecond = tc.getTimeTable()[f'{subPhase}-{stepId}']
    addSecond = targetSecond - stepSecond
    stepAddSecond = tc.getTimeTable()[f'{subPhase}-{stepId}'] + addSecond
    command = simComdLib.command5F1C(seq, subPhase, stepId, stepAddSecond)
    msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
    logging.info(f'設定秒數:{subPhase}-{stepId}，由{stepSecond}秒，增加{addSecond}秒，到達{targetSecond}')
    try:
        r.publish('queState2Tc',msg)
        tc.setCycleChange("5F1C",seq,1,addSecond)
    except Exception as e:
        logging.debug("Error:無法分派訊息至sendCmdQueue")
        logging.debug(e)

def setBranchGreenTime():
    '''
    設定支道路燈時間為TOD值
    '''
    if tc.getPlanId() in ("22", "23", "24", "25", "32", "33", "34"):
        tc.getTimeTable()['3-1'] = TimeTableSecond[tc.getPlanId()]['3-1']
        tc.getTimeTable()['3-2'] = TimeTableSecond[tc.getPlanId()]['3-2']
        tc.getTimeTable()['3-3'] = TimeTableSecond[tc.getPlanId()]['3-3']
        tc.getTimeTable()['3-4'] = TimeTableSecond[tc.getPlanId()]['3-4']
        tc.getTimeTable()['3-5'] = TimeTableSecond[tc.getPlanId()]['3-5']
        tc.getTimeTable()['2-1'] = TimeTableSecond[tc.getPlanId()]['2-1']
        tc.getTimeTable()['2-4'] = TimeTableSecond[tc.getPlanId()]['2-4']
        tc.getTimeTable()['2-5'] = TimeTableSecond[tc.getPlanId()]['2-5']
        tc.branchTimeStep['2-1'] = tc.getTimeTable()['2-1']
        tc.branchTimeStep['2-4'] = tc.getTimeTable()['2-4']
        tc.branchTimeStep['2-5'] = tc.getTimeTable()['2-5']
        tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
        tc.branchTimeStep['3-2'] = tc.getTimeTable()['3-2']
        tc.branchTimeStep['3-3'] = tc.getTimeTable()['3-3']
        tc.branchTimeStep['3-4'] = tc.getTimeTable()['3-4']
        tc.branchTimeStep['3-5'] = tc.getTimeTable()['3-5']
    elif tc.getPlanId() in ("21","31"):
        tc.getTimeTable()['2-1'] = TimeTableSecond[tc.getPlanId()]['2-1']
        tc.getTimeTable()['2-2'] = TimeTableSecond[tc.getPlanId()]['2-2']
        tc.getTimeTable()['2-3'] = TimeTableSecond[tc.getPlanId()]['2-3']
        tc.getTimeTable()['2-4'] = TimeTableSecond[tc.getPlanId()]['2-4']
        tc.getTimeTable()['2-5'] = TimeTableSecond[tc.getPlanId()]['2-5']
        tc.branchTimeStep['2-1'] = tc.getTimeTable()['2-1']
        tc.branchTimeStep['2-2'] = tc.getTimeTable()['2-2']
        tc.branchTimeStep['2-3'] = tc.getTimeTable()['2-3']
        tc.branchTimeStep['2-4'] = tc.getTimeTable()['2-4']
        tc.branchTimeStep['2-5'] = tc.getTimeTable()['2-5']

def syncTimeEvent(event):
    '''
    與號誌控制器進行固定時間的批次任務
    '''
    syncTimeBase = datetime.datetime.now() - datetime.timedelta(seconds=540)
    set5F10Base  =  datetime.datetime.now()
    set5F3FBase  = datetime.datetime.now()
    set5F48Base  =  datetime.datetime.now()
    set5F4CBase  =  datetime.datetime.now()    
    aiTimeBase = datetime.datetime.now()
    
    while not event.isSet():
        timer_start = time.time()
        event.wait(1)
        # 設定控制策略
        if timeCountDown(set5F10Base,300):
            set5F10Base = datetime.datetime.now()
            command = simComdLib.command5F10(tc.getSeq(), ControlStrategy)
            msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
            try:
                logging.info("設定控制策略")
                r.publish('queState2Tc',json.dumps(msg))
            except Exception as e:
                logging.debug("Error:無法分派5F10訊息")
                logging.debug(e)
                
            event.wait(3)   
            command =simComdLib.command5F3F(tc.getSeq(),2,1)
            try:
                logging.info("設定回傳秒數5F3F")
                msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
                r.publish('queState2Tc',json.dumps(msg))
            except Exception as e:
                logging.debug("Error:無法分派5F3F訊息")
                logging.debug(e)

        # 查詢時制計畫編號
        if timeCountDown(set5F48Base,25):
            set5F48Base = datetime.datetime.now()
            command = simComdLib.command5F48(tc.getSeq())
            msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
            try:
                r.publish('queState2Tc',json.dumps(msg))
            except Exception as e:
                logging.debug("Error:無法查詢時制計畫編號5F48訊息")
                logging.debug(e)                

        # 強制查詢當前步階秒數5F4C
        if timeCountDown(set5F4CBase,10):
            set5F4CBase = datetime.datetime.now()
            command = simComdLib.command5F4C(tc.getSeq())
            msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
            try:
                r.publish('queState2Tc',json.dumps(msg))
            except Exception as e:
                logging.debug("Error:無法查詢時制計畫編號5F4C訊息")
                logging.debug(e)                

        # 確認Ai影像有回傳資料
        if timeCountDown(aiTimeBase,20):
            aiTimeBase = datetime.datetime.now()
            checkAiBack = True
            for addr in RsDeviceNameList:
                if aiDevice.getAiBackTime(addr) + datetime.timedelta(seconds=20) < datetime.datetime.now():
                    logging.info(f'Error:{addr}設備的辨識軟體沒有傳送資料~~')
                    checkAiBack = False
                    logging.info("Ai辨識沒有回傳.....感應性號誌系統暫停止運作")
                    aiDevice.setAiBackStatus(False)
                    aiDevice.setAiRestart(False)

            #確認Ai辨識是否正常回復了
            aiDevice.setAiBackStatus(checkAiBack)

        #查詢回報週期
        # if timeCountDown(set5F6FBase,1800):
        #     set5F6FBase = datetime.datetime.now()
        #     command = simComdLib.command5F6F(tc.getSeq())
        #     msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
        #     try:                
        #         r.publish('queState2Tc',json.dumps(msg))
        #     except Exception as e:
        #         logging.debug("Error:查詢回報週期error")
        #         logging.debug(e)


        #同步控制器時間
        if timeCountDown(syncTimeBase,3600):
            syncTimeBase = datetime.datetime.now()
            command = simComdLib.command0F42(tc.getSeq())
            msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
            try:
                r.publish('queState2Tc',json.dumps(msg))
            except Exception as e:
                logging.debug("Error:Send sync Time") 
                logging.debug(e)
                
        timer_end = time.time()
        logging.info(f'syncTimeEvent:{timer_end-timer_start}')

def preCycleStay(pedNum,carNum):
    '''計算目前並統計前二週期在路邊停留的行人及行車數量
    '''
    now = datetime.datetime.now().timestamp()
    actuatePedestrianTime, actuatePedestrianNum, actuatePedestrianOnRoadTime, actuatePedestrianOnRoadNum, actuateCarTime, actuateCarNum, actuateCarOnRoadTime, actuateCarOnRoadNum = tc.getActuate()
    CycleDetectStats["detectRec"][now] = {"pedStay":actuatePedestrianNum,    #偵測到的行人觸動數
                                          "carStay":actuateCarNum}           #偵測到的行車觸動數    

def preCycleTotal():
    '''計算目前並統計前二週期停留的行人及行車數量
    '''
    now = datetime.datetime.now().timestamp()
    PedTotal = 0
    CarTotal = 0 
    for baseTime,item in CycleDetectStats["detectRec"].copy().items():
        if (baseTime > (now - 100)):  #比較過去100秒的觸動情況
            PedTotal += item["pedStay"]
            CarTotal += item["carStay"]
        if baseTime < now - 500:
            del CycleDetectStats["detectRec"][baseTime]
    CycleDetectStats["cyPedTotal"] = PedTotal
    CycleDetectStats["cyCarTotal"] = CarTotal

def getInitSecond():
    '''
    初始化號誌控器，從幹道綠燈做為啟始
    '''
    planId = tc.getPlanId()
    step1_1Sec = 60
    if planId in ("21", "31"):
        step1_1Sec = 60
    elif planId in ("22", "24"):
        step1_1Sec = 75
    elif planId in ("23", "25"):
        step1_1Sec = 40
    elif planId in ("32", "33"):
        step1_1Sec = 70
    elif planId in ("34", ):
        step1_1Sec = 45
    return  step1_1Sec

def initStart(event):
    '''
    初始化半觸動號誌
    '''
    global ControlStrategy
    global gblClearActuateStatus

    # 設定控制模式 5F + 10
    downControlStrategy = False
    while (downControlStrategy is False):
        command = simComdLib.command5F10(tc.getSeq(), ControlStrategy)
        msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
        try:
            r.publish('queState2Tc',json.dumps(msg))
        except Exception as e:
            logging.debug("Error:無法分派5F10訊息")
            logging.debug(e)            
        event.wait(10)

        command = simComdLib.command5F4C(tc.getSeq())
        msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
        try:
            r.publish('queState2Tc',json.dumps(msg))
        except Exception as e:
            logging.info("Error:無法分派5F4C訊息")
            logging.debug(e)            
        event.wait(5)

        if tc.getControlStrategy() == ControlStrategy:
            downControlStrategy = True
            logging.info("切換控制策略成功")
        else:
            logging.info(f'無法切換控制策略...再試一次-{tc.getControlStrategy()}')

    # 設定步階回傳秒數，目前為每秒回傳
    command =simComdLib.command5F3F(tc.getSeq(),2,1)
    try:
        logging.info("設定回傳秒數5F3F")
        msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
        r.publish('queState2Tc',json.dumps(msg))
    except Exception as e:
        logging.debug("Error:無法分派5F3F訊息")
        logging.debug(e)        

    # 查詢當前執行的時制計畫編號 5F+48 -> 5F+C8
    planId = "-1"

    while (planId == "-1"):
        command = simComdLib.command5F48(tc.getSeq())
        msg={"time":int(datetime.datetime.now().timestamp()),"data":command.hex()}
        try:
            logging.info("查詢時制計畫內容5F48")
            r.publish('queState2Tc',json.dumps(msg))

        except Exception as e:
            logging.debug("Error:無法查詢時制計畫內容5F48訊息")
            logging.debug(e)            

        event.wait(10)
        planId = tc.getPlanId()

    step1_1Sec = 60    
    stepId = "2-191"
    stepRemainSec = 0
    while (tc.getPlanId() == "-1") or (stepId != "1-1") or (stepRemainSec < step1_1Sec):
        # 初始化號誌控器，從幹道綠燈做為啟始
        step1_1Sec = getInitSecond()
        stepId,stepRemainSec,_stepReportTime = tc.getStepParameter()
        # 等待tc參數初始化完成
        logging.info(f'應用程式重新初始化中...{tc.getPlanId()},{stepId},{stepRemainSec}')
        event.wait(1)

    # 週期開始時更新支道綠燈的秒數計算參數
    setBranchGreenTime()

    # 系統重新啟動後行人、行車有預設值
    preCycleStay(1,1)
    
    #清除累積的偵測數值
    tc.clearActuate()

    logging.info(f'應用程式初始化完成')

def main():
    global gblClearActuateStatus
    global gblSetGreenBranch

    logTimeBase = 0
    logTime = time.time()

    actuateStatusBool = True        #判斷是否在感應性號誌啟動狀態
    ext5F1CBool = False             #判斷5F1C是否正常運作

    rptCycle = {    #週期執行狀況報表物件
        "rptCycleStartBool" : False,                   #週期是否啟動
        "rptCycleReportBool" : False,                  #是否已寫下本週期紀錄    
        "rptCycleMainRoadStartTime" : datetime.datetime.now(),          #紀錄週期開始時間    
        'rptCycleStartTime':int(datetime.datetime.timestamp(datetime.datetime.now())),      #紀錄週期開始時間timestamp
        "rptCycleEndTime" : int(datetime.datetime.timestamp(datetime.datetime.now())),      #紀錄週期結束時間timestamp
        "rptCycleActuateType" : "",                     #紀錄1-1時偵測為行人最短綠還是行車最短綠
        "rptPedestrianNum" : 0,                    #紀錄支道行人延長次數
        "rptCarNum" : 0,                           #紀錄支道行車延長次數
        "rptMainRoadExtend" : 0,                   #紀錄幹道延長次數
        "rptBranchGreenTime" : 0,                  #紀錄支道綠燈時間

        "rptCycleMainRoadEndTime" : datetime.datetime.now(),  #紀錄主幹道綠燈結束開始時間
        "rptCycleMainRoadTableSec" : 0,             #紀錄主幹道綠燈總秒數
        "rptCycleLeftGreenSec" : 0,                 #紀錄上一週期剩餘綠燈時間/可以調撥到幹道的時間
        'activeId': int(datetime.datetime.timestamp(datetime.datetime.now())),
        'triggerType':0,
        'triggerCar': 0,
        'triggerPerson': 0,
        'without-triger':0,
        'phaseStartTime' : int(datetime.datetime.timestamp(datetime.datetime.now())),
        'phaseEndTime' : int(datetime.datetime.timestamp(datetime.datetime.now())),
        'phaseNum':0,
        'phase1':0,
        "pedgreenflash1":0,
        'pedred1':0,
        'phase2':0,
        "pedgreenflash2":0,
        'pedred2':0,        
        'phase3':0,
        "pedgreenflash3":0,
        'pedred3':0,        
        'addGreenSecond':0    
    }
    def rptCycleReset(rptObj):
        '''
        週期開始時初始化報表相關參數
        '''        
        now = datetime.datetime.now()
        rptObj["rptCycleStartBool"] = True
        rptObj["rptCycleReportBool"] = False
        rptObj["rptCycleStartBool"] = True
        rptObj["rptCycleMainRoadStartTime"] = now
        rptObj["rptCycleEndTime"] = now
        rptObj["rptCycleActuateType"] = ""
        rptObj["rptPedestrianNum"] = 0
        rptObj["rptCarNum"] = 0
        rptObj["rptMainRoadExtend"] = 0
        rptObj["rptBranchGreenTime"] = 0

        rptObj["rptCycleMainRoadEndTime"] =  now   #紀錄主幹道綠燈結束時間/支道綠燈開始時間
        rptObj["rptCycleMainRoadTableSec"] = 0
        
        rptObj["activeId"] = int(datetime.datetime.timestamp(now))
        rptObj["triggerType"] = 0
        rptObj["triggerCar"] = 0
        rptObj["triggerPerson"] = 0
        rptObj["without-triger"] =0
        rptObj["phaseNum"] = 0
        rptObj["phaseStartTime"] = datetime.datetime.timestamp(now)
        rptObj["phaseEndTime"] = datetime.datetime.timestamp(now)
        rptObj["phase1"] = 0
        rptObj["pedgreenflash1"]=0
        rptObj['pedred1']=0
        rptObj["phase2"] = 0
        rptObj["pedgreenflash2"]=0
        rptObj['pedred2']=0
        rptObj["phase3"] = 0
        rptObj["pedgreenflash3"]=0
        rptObj['pedred3']=0
        rptObj["addGreenSecond"] = 0
        rptObj["rptCycleStartTime"] = int(datetime.datetime.timestamp(now))
        rptObj["rptCycleEndTime"] = int(datetime.datetime.timestamp(now))

        return rptObj

    def rptCycleOutput(rptObj):
        '''
        週期結束時輸出報表相關參數
        '''
        startTime = rptObj["rptCycleMainRoadStartTime"]
        rptCycleMainRoadStartTimeStr = datetime.datetime.strftime(startTime, '%Y-%m-%d %H:%M:%S')
        rptCycleEndTimeStr = datetime.datetime.fromtimestamp(rptObj["rptCycleEndTime"]).strftime('%Y-%m-%d %H:%M:%S')
        logging.info(f'報表1,{rptCycleMainRoadStartTimeStr},{rptCycleEndTimeStr},{tc.getCycleTime()},{rptObj["rptCycleActuateType"]},{rptObj["rptMainRoadExtend"]},{rptObj["rptPedestrianNum"]},{rptObj["rptCarNum"]},{rptObj["rptBranchGreenTime"]}')
        
        rptObj["rptCycleReportBool"] = True
        rptObj["rptCycleStartBool"] = False

        addGreenSecond = rptObj["rptCycleLeftGreenSec"]
        data = {                
                'activeId': rptObj["activeId"],
                'triggerType': rptObj["triggerType"],
                'triggerCar': rptObj["triggerCar"],
                'triggerPerson': rptObj["triggerPerson"],
                'withoutTriger':rptObj["without-triger"],
                'phaseNum':rptObj["phaseNum"],
                'phase1':rptObj["phase1"],
                'pedgreenflash1':rptObj["pedgreenflash1"],
                'pedred1':rptObj['pedred1'],
                'phase2':rptObj["phase2"],
                'pedgreenflash2':rptObj["pedgreenflash2"],
                'pedred2':rptObj['pedred2'],
                'phase3':rptObj["phase3"],
                'pedgreenflash3':rptObj["pedgreenflash3"],
                'pedred3':rptObj['pedred3'],
                'addGreenSecond':addGreenSecond,
                'startTime':rptObj["rptCycleStartTime"],
                'endTime': rptObj["rptCycleEndTime"]}
        
        r.publish('queState2Center',json.dumps(data))
        return rptObj

    rptSubphase= {
        "rptSubphaseStartBool" : False,                    #分相是否開始
        "rptSubphaseReportBool" : False,                   #是否已紀錄上一分相資料
        "rptSubphaseStartTime" : datetime.datetime.now(),  #分相開始時間
        "rptMainStartSecond" : 0,          #紀錄步階1-1的秒數
        "rptSubphaseYellow" : 0,           #分相黃秒數
        "rptSubphaseRed" : 0,              #分相全紅秒數
        "rptSubphasePedestrianGreen" : 0,  #分相行人綠秒數
        "rptSubphasePedestrianRed" : 0    #分相行人紅秒數    
    }  
    def rptSubphaseReset(rptObj,phaseId):
        '''
        分相開始時初始化報表相關參數
        '''         
        rptObj["rptSubphaseReportBool"] = False
        rptObj["rptSubphaseStartTime"] = datetime.datetime.now()
        rptObj["rptMainStartSecond"] = tc.getTimeTable()[phaseId]
        rptObj["rptSubphaseStartBool"] = True #開始phaseId分相
        return rptObj

    def rptSubphaseOutput(rptObj,phaseOrder):
        '''
        分相結束時輸出報表相關參數
        '''   
        rptSubphaseStartTimeStr = datetime.datetime.strftime(rptObj["rptSubphaseStartTime"], '%Y-%m-%d %H:%M:%S')
        logging.info(f'報表2,{rptSubphaseStartTimeStr},{tc.getCycleTime()},{tc.getOffset()},{tc.getPlanId()},{tc.getSubPhaseCount()},{tc.getPhaseOrder()},{phaseOrder},{rptObj["rptSubphaseYellow"]},{rptObj["rptSubphaseRed"]},{rptObj["rptSubphasePedestrianGreen"]}, {rptObj["rptSubphasePedestrianRed"]}')
        rptObj["rptSubphaseReportBool"] = True
        rptObj["rptSubphaseStartBool"] = False
        return rptObj

    event = threading.Event()
    syncTimeThread = threading.Thread(target=syncTimeEvent, args=(event,))
    syncTimeThread.start()
    receActiveThread = threading.Thread(target=receTcEvent, args=(event,))
    receActiveThread.start()
    receAiThread = threading.Thread(target=receAiEvent, args=(event,))
    receAiThread.start()
    # autoSendThread = threading.Thread(target=autoSendAPI,args=(event,))
    # autoSendThread.start()
    # centerStartThread= threading.Thread(target=centerStartControl,args=(event,))
    # centerStartThread.start()

    initStart(event)

    while not event.isSet():
        try:
            timer_start = time.time()
            actuateStatusBool = True   #判斷半觸動號誌是否在啟動狀態
            PoliceStatus = tc.getPoliceStatus()
            PoliceRestore = tc.getPoliceRestore()
            PlanChange = tc.getPlanChange()
            AiBackStatus = aiDevice.getAiBackStatus()
            AiRestart = aiDevice.getAiRestart()
            CenterStartStatus = tyCenter.getCenterControl()

            extendMaxCycle = 1  #最多延長1次

            planId = tc.getPlanId()
            stepId, stepRemainSec, _stepReportTime = tc.getStepParameter()
            actuatePedestrianTime, actuatePedestrianNum, actuatePedestrianOnRoadTime, actuatePedestrianOnRoadNum, actuateCarTime, actuateCarNum, actuateCarOnRoadTime, actuateCarOnRoadNum = tc.getActuate()
            mainRoadExtend = tc.getMainRoadExtend()
            branchExtendNum = tc.getBranchExtendNum()
            timer_end = time.time()
            logging.info(f'main1:{timer_end-timer_start}')


            timer_start = time.time()
            #針對中心下達開關指令處理
            if CenterStartStatus is False: 
                logging.info("接收到中心下達停止感應性號誌運作")
                tyCenter.setCenterControlReport("stopping")
            if CenterStartStatus is False and stepId == "1-1":                
                while(CenterStartStatus is False):  # 
                    tyCenter.setCenterControlReport("stopped")
                    CenterStartStatus = tyCenter.getCenterControl()
                    event.wait(3)
                tyCenter.setCenterControlReport("starting")
                initStart(event)
                tyCenter.setCenterControlReport("online")
                rptCycle = rptCycleReset(rptCycle)
                rptCycle["rptCycleLeftGreenSec"] = 0 #上一週期移撥綠燈歸零
                rptSubphase = rptSubphaseReset(rptSubphase,'1-1')

            #針對手操燈進行處理
            if PoliceStatus is True:
                logging.info("手操燈執行中，停止執行感應性號誌運作")
                while(PoliceRestore is False):  # 連動手操燈時不執行
                    PoliceRestore = tc.getPoliceRestore()
                    event.wait(1)
                if PoliceRestore is True:
                    tc.setPoliceStatus(False)
                    tc.setPoliceRestore(False)
                    event.wait(10)
                    initStart(event)
                    rptCycle = rptCycleReset(rptCycle)
                    rptCycle["rptCycleLeftGreenSec"] = 0 #上一週期移撥綠燈歸零
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1')

            #針對Ai辨識回應
            if (AiBackStatus is False) and (AiRestart is False):
                actuateStatusBool = False
            elif  (AiBackStatus is True) and (AiRestart is False):
                if stepId != "1-1":
                    actuateStatusBool = False
                elif stepId == "1-1":  #重新開始半觸動
                    logging.info('Ai辨識恢復，重啟感應性號誌')
                    aiDevice.setAiRestart(True)
                    initStart(event)
                    rptCycle = rptCycleReset(rptCycle)
                    rptCycle["rptCycleLeftGreenSec"] = 0 #上一週期移撥綠燈歸零
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1')

            timer_end = time.time()
            logging.info(f'main2:{timer_end-timer_start}')

            # 閃黃燈時制，不啟動半觸動
            if planId in ("26", "35"):
                actuateStatusBool = False
                tc.setPlanChange(False)
                PlanChange = False
                event.wait(1)
                continue


            #針對時制轉換回應
            if PlanChange is True :
                actuateStatusBool = False
                step1_1Sec = getInitSecond() 
                stepId,stepRemainSec,_stepReportTime = tc.getStepParameter()
                if gblSetGreenBranch is True: #在偵測到新時制計畫編號時，重新讀一次
                    setBranchGreenTime()
                    gblSetGreenBranch = False
                inMainRoadExtendDuration = False
                if mainRoadExtend > 0 and (datetime.datetime.now()-tc.getPlanChangeStartTime()).total_seconds() < tc.getCycleTime() + 10 :  # 在主幹道無觸動延長時遇到1-1進行時制轉換的排除
                    inMainRoadExtendDuration = True
                if (tc.getPlanId() not in ("26", "35")) and  (stepId == "1-1") and (stepRemainSec > step1_1Sec) and (inMainRoadExtendDuration is False):  #結束快速連鎖時間了
                    tc.setPlanChange(False)
                    initStart(event)
                    actuateStatusBool = True
                    rptCycle = rptCycleReset(rptCycle)
                    rptCycle["rptCycleLeftGreenSec"] = 0 #上一週期移撥綠燈歸零
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1')
                    gblSetGreenBranch = False

            # 150秒週期且1-1為85秒，幹道的綠燈步階開始
            if planId in ("22","24","32","33") and stepId == "1-1":
                timer_start = time.time()
                rptCycle["phaseNum"] = 3    #時制轉換時可能會遇到rptCycle["rptCycleStartBool"]=True
                ext5F1CBool = False
                if rptCycle["rptCycleStartBool"] is False:                    
                    nowTimestamp = datetime.datetime.timestamp(datetime.datetime.now())
                    if (actuateStatusBool is True) and (rptCycle["rptCycleEndTime"] < nowTimestamp -100):
                        phaseStartTime = rptCycle["rptCycleEndTime"]
                    else:
                        phaseStartTime = datetime.datetime.timestamp(datetime.datetime.now())
                    rptCycle = rptCycleReset(rptCycle)
                    rptCycle["phaseStartTime"] = phaseStartTime
                    rptCycle["rptCycleMainRoadTableSec"] = TimeTableSecond[planId]['1-1'] + TimeTableSecond[planId]['1-2'] + TimeTableSecond[planId]['1-3'] + \
                                            TimeTableSecond[planId]['1-4'] + TimeTableSecond[planId]['1-5'] + \
                                            TimeTableSecond[planId]['2-1'] + TimeTableSecond[planId]['2-4'] + TimeTableSecond[planId]['2-5'] 
                
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1') #開始分相1
                    rptSubphase["rptMainStartSecond"]=  tc.getTimeTable()['1-1']
                    
                # tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']  #良基控制器會保留上一週期值，故需保留前週期對1-1設定值
                if gblSetGreenBranch is False:
                    setBranchGreenTime()
                    gblSetGreenBranch = True
                if gblClearActuateStatus is False:
                    #統計上二週期的偵測路上行人、行車數量
                    preCycleTotal()
                    #初始化指令是否執行統計物件
                    tc.intCycleChange()
                    tc.clearActuate()
                    gblClearActuateStatus = True

                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    # 情境：週期中無行人及行車觸動訊號，延長
                    if ((actuatePedestrianNum == 0) and (CycleDetectStats["cyPedTotal"] == 0)) and (actuateCarNum == 0) and (mainRoadExtend == 0) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=140)
                        targetSecond = tc.getTimeTable()['1-1'] + 150
                        currentStepSecondChange(tc.getSeq(), 1, 1, targetSecond)
                        tc.getTimeTable()['1-1'] = targetSecond
                        tc.stepSec = targetSecond
                        tc.setMainRoadExtend(1)
                        rptCycle["without-triger"] += 1
                        logging.info(f"**幹道綠燈:1-1延長150秒,第{tc.getMainRoadExtend()}次")
                    # 情境：超過無行人及無行車觸動訊號的延長週期，執行行人最短綠
                    elif (tc.getActuatePedestrianLevel() == 0) and (actuatePedestrianNum == 0) and (actuateCarNum == 0) and (tc.actuateAcceptTime < datetime.datetime.now()) and (mainRoadExtend == extendMaxCycle) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now()
                        tc.setActuatePedestrianLevel(1)
                        logging.info(f"**幹道綠燈:1-1延長150秒,到達上限{tc.getMainRoadExtend()}次")
                        rptCycle["triggerType"] = 0
                        rptCycle["rptCycleActuateType"] = "延長上限"
            elif planId in ("22","24","32","33") and stepId in ("1-2", "1-3"):
                # 情境：幹道綠燈收到行人觸動訊號或前二個週期有行人通過路口
                timer_start = time.time()
                if ((actuatePedestrianNum > 0) or (CycleDetectStats["cyPedTotal"] > 0)) and (tc.getActuatePedestrianLevel() == 0) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                    tc.setActuatePedestrianLevel(1)
                    rptCycle["triggerType"] = 1
                    rptCycle["rptCycleActuateType"] = "行人最短綠"
                # 情境：幹道綠燈只有行車觸動訊號(但無行人觸動)，而且前二個週期有車通過路口
                elif (tc.getActuatePedestrianLevel() == 0)  and (CycleDetectStats["cyCarTotal"] > 0 or (actuateCarNum > 0)) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                    planStepSecondChange(tc.getSeq(), 3, 1, 2)
                    tc.getTimeTable()['3-1'] = 2
                    tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
                    tc.setActuateCarLevel(1)
                    rptCycle["triggerType"] = 2
                    rptCycle["rptCycleActuateType"] = "行車最短綠"
            elif planId in ("22","24","32","33") and stepId in ("1-4", "1-5"):
                timer_start = time.time()
                gblSetGreenBranch = False
                gblClearActuateStatus = False
                tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']
                rptCycle["rptMainRoadExtend"] = tc.getMainRoadExtend()
                rptCycle["pedgreenflash1"] = tc.getTimeTable()['1-2']
                rptCycle["pedred1"] = tc.getTimeTable()['1-3']
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['1-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['1-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * tc.getCycleTime() + tc.getTimeTable()['1-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['1-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,1)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=7))
                    rptCycle["phase1"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * 150 + tc.getTimeTable()['1-2'] + tc.getTimeTable()['1-3'] + tc.getTimeTable()['1-4'] + tc.getTimeTable()['1-5']
            elif planId in ("22","24","32","33") and stepId in ("2-1",) :
                timer_start = time.time()
                if rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() == 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18:
                        tc.getTimeTable()['3-1'] = 20
                        tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
                elif rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() > 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18 + tc.getMainRoadExtend() * 150:
                        tc.getTimeTable()['3-1'] = 20
                        tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']

                # if tc.getMainRoadExtend() == 1 and tc.getTimeTable()['2-1'] == TimeTableSecond[planId]['2-1']:
                #     targetSecond = tc.getTimeTable()['2-1'] + 5
                #     planStepSecondChange(tc.getSeq(), 2, 1, targetSecond)
                #     tc.getTimeTable()['2-1'] = targetSecond
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'2-1') #開始分相2
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相1的最後時間 
            elif planId in ("22","24","32","33") and stepId in ("2-4","2-5",):
                timer_start = time.time()
                if rptSubphase["rptSubphaseReportBool"] is False :
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['2-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['2-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = tc.getTimeTable()['2-1'] 
                    rptSubphase["rptSubphasePedestrianRed"] = 0
                    rptSubphase = rptSubphaseOutput(rptSubphase,2)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=7))
                    # rptCycle["phase2"] = int(rptCycle["phaseEndTime"] - rptCycle["phaseStartTime"])
                    rptCycle["phase2"] = tc.branchTimeStep['2-1'] + tc.branchTimeStep['2-4'] + tc.branchTimeStep['2-5']
                    rptCycle["pedgreenflash2"] = 0
                    rptCycle["pedred2"] = 0
                if gblClearActuateStatus is False:
                    #統計幹道通行時間行人、行車數量
                    preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                    tc.clearActuate()
                    gblClearActuateStatus = True
            elif planId in ("22","24","32","33") and stepId in ("3-1", ):
                timer_start = time.time()
                gblClearActuateStatus = False   # 設定清空回復值為False
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'3-1') #開始分相3
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now()
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相2的最後時間 
                    tc.intCycleChange() #初始化支道執行秒數延長統計物件
            elif planId in ("22","24","32","33") and stepId == "3-2":  # 如果是支道的綠燈步階
                timer_start = time.time()
                if rptSubphase["rptSubphaseStartBool"] is False:    #如果3-1被控制器跳過時
                    rptSubphase = rptSubphaseReset(rptSubphase,'3-1') #開始分相3
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now() -datetime.timedelta(seconds=2)
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相2的最後時間 
                    tc.intCycleChange() #初始化支道執行秒數延長統計物件                
                if (stepRemainSec >5 ):  # 設定觸動時間，以避免後續一觸動就啟動延長
                    tc.actuateAcceptTime = datetime.datetime.now() 
                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    tc.extendMaxSecond = 33 - tc.branchTimeStep['3-1'] - tc.branchTimeStep['3-2'] - tc.branchTimeStep['3-3'] - tc.branchTimeStep['3-4']- tc.branchTimeStep['3-5']
                    if tc.extendMaxSecond >= 3 and (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=3)
                        stepSecond = tc.getTimeTable()['3-2']
                        planStepSecondChange(tc.getSeq(), 3, 2, stepSecond + 3)
                        tc.getTimeTable()['3-2'] = stepSecond + 3
                        tc.branchTimeStep['3-2'] = tc.getTimeTable()['3-2']
                        tc.setBranchExtendNum(1)
                        rptCycle["triggerCar"] +=1
            elif planId in ("22","24","32","33") and stepId == "3-3":
                timer_start = time.time()
                gblClearActuateStatus = False   #3-1可能沒有執行到，所以清空回復值置於此
                tc.extendMaxSecond = 33 - tc.branchTimeStep['3-1'] - tc.branchTimeStep['3-2'] - tc.branchTimeStep['3-3'] - tc.branchTimeStep['3-4']- tc.branchTimeStep['3-5']
                if tc.extendMaxSecond >= 3 and (stepRemainSec >= 3) and (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                    tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=3)
                    stepSecond = tc.getTimeTable()['3-3']
                    currentStepSecondChange(tc.getSeq(), 3, 3, stepSecond + 3)
                    tc.getTimeTable()['3-3'] = stepSecond + 3
                    tc.branchTimeStep['3-3'] = tc.getTimeTable()['3-3']
                    tc.setBranchExtendNum(1)
                    rptCycle["triggerCar"] +=1
            elif planId in ("22","24","32","33") and stepId in ("3-4", "3-5"):
                timer_start = time.time()
                gblClearActuateStatus = False
                #統計支道時間是否有行人、行車通過路口
                preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                #統計此週期的執行報表              
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['3-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['3-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = tc.getTimeTable()['3-1']  + tc.getTimeTable()['3-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['3-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,3)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=5))
                    calBranchGreenTime = tc.branchTimeStep['3-1'] + tc.branchTimeStep['3-2'] + tc.branchTimeStep['3-3'] + tc.branchTimeStep['3-4']+ tc.branchTimeStep['3-5']
                    extendTotalGreenTime = tc.getCycleChange('5F1C')
                    if rptCycle["rptCycleActuateType"] == "行車最短綠":
                        calBranchGreenTime = 33 - 18 + extendTotalGreenTime
                    new1_1StepSecond = TimeTableSecond[planId]['1-1'] + (33 - calBranchGreenTime)
                    if (tc.getTimeTable()['1-1'] != new1_1StepSecond) and (actuateStatusBool is True):
                        stepSecond = new1_1StepSecond
                        planStepSecondChange(tc.getSeq(), 1, 1, stepSecond)
                        tc.getTimeTable()['1-1'] = stepSecond
                    rptCycle["rptBranchGreenTime"] = calBranchGreenTime

                if rptCycle["rptCycleReportBool"] is False :
                    rptCycle["pedgreenflash3"] = tc.branchTimeStep['3-2']
                    rptCycle["pedred3"] = tc.branchTimeStep['3-3']                    
                    rptCycle["phase3"] = calBranchGreenTime                    
                    rptCycle["rptCycleEndTime"] = int(rptCycle["phaseEndTime"])
                    rptCycle["rptCycleLeftGreenSec"] +=  rptCycle["rptMainRoadExtend"] * tc.getCycleTime()
                    rptCycle = rptCycleOutput(rptCycle)
                    #待報表輸出後，再將調撥時間存入
                    rptCycle["rptCycleLeftGreenSec"] = 33- calBranchGreenTime

            # 120秒週期狀況，三週期   *******
            if planId in ("23","25","34") and stepId == "1-1":
                rptCycle["phaseNum"] = 3
                ext5F1CBool = False
                if rptCycle["rptCycleStartBool"] is False:
                    nowTimestamp = datetime.datetime.timestamp(datetime.datetime.now())
                    if (actuateStatusBool is True) and (rptCycle["rptCycleEndTime"] < nowTimestamp -90):
                        phaseStartTime = rptCycle["rptCycleEndTime"]
                    else:
                        phaseStartTime = datetime.datetime.timestamp(datetime.datetime.now())
                    rptCycle = rptCycleReset(rptCycle)                    
                    rptCycle["phaseStartTime"] = phaseStartTime                 
                    rptCycle["rptCycleMainRoadTableSec"] = TimeTableSecond[planId]['1-1'] + TimeTableSecond[planId]['1-2'] + TimeTableSecond[planId]['1-3'] + \
                                            TimeTableSecond[planId]['1-4'] + TimeTableSecond[planId]['1-5'] + \
                                            TimeTableSecond[planId]['2-1'] + TimeTableSecond[planId]['2-4'] + TimeTableSecond[planId]['2-5'] 

                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1') #開始分相1
                    rptSubphase["rptMainStartSecond"]=  tc.getTimeTable()['1-1']
                
                # tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']   #因應良基控制器修正

                if gblSetGreenBranch is False:
                    setBranchGreenTime()
                    gblSetGreenBranch = True
                if gblClearActuateStatus is False:
                    #統計上二週期的偵測路上行人、行車數量
                    preCycleTotal()
                    #初始化指令是否執行統計物件
                    tc.intCycleChange()                    
                    tc.clearActuate()
                    gblClearActuateStatus = True

                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    # 情境：週期中無行人及行車觸動訊號，延長
                    if ((actuatePedestrianNum == 0) or (CycleDetectStats["cyPedTotal"] == 0)) and (actuateCarNum == 0) and (mainRoadExtend == 0) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=110)
                        targetSecond = tc.getTimeTable()['1-1'] + 120
                        currentStepSecondChange(tc.getSeq(), 1, 1, targetSecond)
                        tc.getTimeTable()['1-1'] = targetSecond
                        tc.stepSec = targetSecond
                        tc.setMainRoadExtend(1)
                        rptCycle["without-triger"] += 1
                        logging.info(f"**幹道綠燈:1-1延長120秒,第{tc.getMainRoadExtend()}次")
                    # 情境：超過無行人及無行車觸動訊號的延長週期，執行行人最短綠
                    elif (tc.getActuatePedestrianLevel() == 0) and (actuatePedestrianNum == 0) and (actuateCarNum == 0) and (tc.actuateAcceptTime < datetime.datetime.now()) and (mainRoadExtend == extendMaxCycle) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now()
                        tc.setActuatePedestrianLevel(1)
                        logging.info(f"**幹道綠燈:1-1延長120秒,到達上限{tc.getMainRoadExtend()}次")
                        rptCycle["rptCycleActuateType"] = "延長上限"
                        rptCycle["triggerType"] = 0
            elif planId in ("23","25","34") and stepId in ("1-2", "1-3"):
                # 情境：幹道綠燈收到行人觸動訊號或前二個週期有行人通過路口
                if ((actuatePedestrianNum > 0) or (CycleDetectStats["cyPedTotal"] > 0)) and (tc.getActuatePedestrianLevel() == 0) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                    tc.setActuatePedestrianLevel(1)
                    rptCycle["triggerType"] = 1
                    rptCycle["rptCycleActuateType"] = "行人最短綠"
                # 情境：幹道綠燈只有行車觸動訊號(但無行人觸動)，而且前二個週期有車通過路口
                elif (tc.getActuatePedestrianLevel() == 0)  and (CycleDetectStats["cyCarTotal"] > 0 or (actuateCarNum > 0)) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                    planStepSecondChange(tc.getSeq(), 3, 1, 2)
                    tc.getTimeTable()['3-1'] = 2
                    tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
                    tc.setActuateCarLevel(1)
                    rptCycle["triggerType"] = 2
                    rptCycle["rptCycleActuateType"] = "行車最短綠"
            elif planId in ("23","25","34") and stepId in ("1-4", "1-5"):
                gblSetGreenBranch = False
                gblClearActuateStatus = False
                tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']
                rptCycle["rptMainRoadExtend"] = tc.getMainRoadExtend()
                rptCycle["pedgreenflash1"] = tc.getTimeTable()['1-2']
                rptCycle["pedred1"] = tc.getTimeTable()['1-3']
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['1-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['1-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * tc.getCycleTime() + tc.getTimeTable()['1-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['1-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,1)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=7))
                    # rptCycle["phase1"] = int(rptCycle["phaseEndTime"] - rptCycle["phaseStartTime"])
                    rptCycle["phase1"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * 120 + tc.getTimeTable()['1-2'] + tc.getTimeTable()['1-3'] + tc.getTimeTable()['1-4'] + tc.getTimeTable()['1-5']
            elif planId in ("23","25","34") and stepId in ("2-1",) :
                if rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() == 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18:
                        tc.getTimeTable()['3-1'] = 20
                        tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
                elif rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() > 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18 + tc.getMainRoadExtend() * 120:
                        tc.getTimeTable()['3-1'] = 20
                        tc.branchTimeStep['3-1'] = tc.getTimeTable()['3-1']
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'2-1') #開始分相2
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相1的最後時間 
            elif planId in ("23","25","34") and stepId in ("2-4","2-5"):
                if rptSubphase["rptSubphaseReportBool"] is False :
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['2-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['2-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = tc.getTimeTable()['2-1'] 
                    rptSubphase["rptSubphasePedestrianRed"] = 0
                    rptSubphase = rptSubphaseOutput(rptSubphase,2)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=7))
                    rptCycle["phase2"] = tc.branchTimeStep['2-1'] + tc.branchTimeStep['2-4'] + tc.branchTimeStep['2-5']
                    rptCycle["pedgreenflash2"] = 0
                    rptCycle["pedred2"] = 0                    
                if gblClearActuateStatus is False:
                    #統計幹道通行時間行人、行車數量
                    preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                    tc.clearActuate()
                    gblClearActuateStatus = True
                    # logging.info(f"**支道綠燈:3-1={tc.getTimeTable()['3-1']},3-1={tc.getTimeTable()['4-1']},4-2={tc.getTimeTable()['4-2']},4-3={tc.getTimeTable()['4-3']}")
            elif planId in ("23","25","34") and stepId in ("3-1", ):
                gblClearActuateStatus = False
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'3-1') #開始分相3
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now()
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相2的最後時間 
                    tc.intCycleChange() #初始化支道執行秒數延長統計物件
            elif planId in ("23","25","34") and stepId == "3-2":  # 如果是支道的綠燈步階
                if rptSubphase["rptSubphaseStartBool"] is False:    #如果3-1被控制器跳過時
                    rptSubphase = rptSubphaseReset(rptSubphase,'3-1') #開始分相3
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now() -datetime.timedelta(seconds=2)
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相2的最後時間 
                    tc.intCycleChange() #初始化支道執行秒數延長統計物件
                if (stepRemainSec >5 ):  # 設定觸動時間，以避免後續一觸動就啟動延長
                    tc.actuateAcceptTime = datetime.datetime.now() 
                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    tc.extendMaxSecond = 33 - tc.branchTimeStep['3-1'] - tc.branchTimeStep['3-2'] - tc.branchTimeStep['3-3'] - tc.branchTimeStep['3-4']- tc.branchTimeStep['3-5']
                    if tc.extendMaxSecond >= 3 and (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=3)
                        stepSecond = tc.getTimeTable()['3-2']
                        planStepSecondChange(tc.getSeq(), 3, 2, stepSecond + 3)
                        tc.getTimeTable()['3-2'] = stepSecond + 3
                        tc.branchTimeStep['3-2'] = tc.getTimeTable()['3-2']
                        tc.setBranchExtendNum(1)
                        rptCycle["triggerCar"] +=1
            elif planId in ("23","25","34") and stepId == "3-3":
                gblClearActuateStatus = False   #3-1及4-1、4-2可能不執行，所以清空回復值置於此
                tc.extendMaxSecond = 33 - tc.branchTimeStep['3-1'] - tc.branchTimeStep['3-2'] - tc.branchTimeStep['3-3'] - tc.branchTimeStep['3-4']- tc.branchTimeStep['3-5']
                if (tc.extendMaxSecond >= 3) and (stepRemainSec >= 3):  # 尚有餘裕秒數可以延長
                    # 情境：步階中行車觸動訊號延長                    
                    if (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=2)
                        stepSecond = tc.getTimeTable()['3-3']
                        currentStepSecondChange(tc.getSeq(), 3, 3, stepSecond + 3)
                        tc.getTimeTable()['3-3'] = stepSecond + 3
                        tc.branchTimeStep['3-3'] = tc.getTimeTable()['3-3']
                        tc.setBranchExtendNum(1)
                        rptCycle["triggerCar"] +=1
            elif planId in ("23","25","34") and stepId in ("3-4", "3-5"):
                gblClearActuateStatus = False
                #統計支道時間是否有行人、行車通過路口
                preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                #統計此週期的執行報表    
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['3-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['3-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = tc.getTimeTable()['3-1']  + tc.getTimeTable()['3-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['3-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,3)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=5))

                    calBranchGreenTime = tc.branchTimeStep['3-1'] + tc.branchTimeStep['3-2'] + tc.branchTimeStep['3-3'] + tc.branchTimeStep['3-4']+ tc.branchTimeStep['3-5']                    
                    extendTotalGreenTime = tc.getCycleChange('5F1C')
                    if rptCycle["rptCycleActuateType"] == "行車最短綠":
                        calBranchGreenTime = 33 - 18 + extendTotalGreenTime
                    new1_1StepSecond = TimeTableSecond[planId]['1-1'] + (33 - calBranchGreenTime)
                    if (tc.getTimeTable()['1-1'] != new1_1StepSecond) and (actuateStatusBool is True):
                        stepSecond = new1_1StepSecond
                        planStepSecondChange(tc.getSeq(), 1, 1, stepSecond)
                        tc.getTimeTable()['1-1'] = stepSecond
                    rptCycle["rptBranchGreenTime"] = calBranchGreenTime                  

                if rptCycle["rptCycleReportBool"] is False :
                    rptCycle["pedgreenflash3"] = tc.branchTimeStep['3-2']
                    rptCycle["pedred3"] = tc.branchTimeStep['3-3']
                    rptCycle["phase3"] = calBranchGreenTime                    
                    rptCycle["rptCycleEndTime"] = int(rptCycle["phaseEndTime"])
                    rptCycle["rptCycleLeftGreenSec"] +=  rptCycle["rptMainRoadExtend"] * tc.getCycleTime()
                    rptCycle = rptCycleOutput(rptCycle)
                    #待報表輸出後，再將調撥時間存入
                    rptCycle["rptCycleLeftGreenSec"] = 33- calBranchGreenTime 

            # 120秒週期狀況，二週期   *******
            if planId in ("21","31") and stepId == "1-1":
                rptCycle["phaseNum"] = 2
                ext5F1CBool = False
                if rptCycle["rptCycleStartBool"] is False:
                    nowTimestamp = datetime.datetime.timestamp(datetime.datetime.now())
                    if (actuateStatusBool is True) and (rptCycle["rptCycleEndTime"] < nowTimestamp -90):
                        phaseStartTime = rptCycle["rptCycleEndTime"]
                    else:
                        phaseStartTime = datetime.datetime.timestamp(datetime.datetime.now())
                    rptCycle = rptCycleReset(rptCycle)
                    rptCycle["phaseStartTime"] = phaseStartTime
                    rptCycle["rptCycleMainRoadTableSec"] = TimeTableSecond[planId]['1-1'] + TimeTableSecond[planId]['1-2'] + TimeTableSecond[planId]['1-3'] + \
                                            TimeTableSecond[planId]['1-4'] + TimeTableSecond[planId]['1-5']

                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'1-1') #開始分相1
                    rptSubphase["rptMainStartSecond"]=  tc.getTimeTable()['1-1']

                # tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']         #因應良基控制器修正

                if gblSetGreenBranch is False:
                    setBranchGreenTime()
                    gblSetGreenBranch = True
                if gblClearActuateStatus is False:
                    #統計上二週期的偵測路上行人、行車數量
                    preCycleTotal()
                    #初始化指令是否執行統計物件
                    tc.intCycleChange()                    
                    tc.clearActuate()
                    gblClearActuateStatus = True

                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    # 情境：週期中無行人及行車觸動訊號，延長
                    if ((actuatePedestrianNum == 0) or (CycleDetectStats["cyPedTotal"] == 0)) and (actuateCarNum == 0) and (mainRoadExtend == 0) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=110)
                        targetSecond = tc.getTimeTable()['1-1'] + 120
                        currentStepSecondChange(tc.getSeq(), 1, 1, targetSecond)
                        tc.getTimeTable()['1-1'] = targetSecond
                        tc.stepSec = targetSecond
                        tc.setMainRoadExtend(1)
                        rptCycle["without-triger"] += 1
                        logging.info(f"**幹道綠燈:1-1延長120秒,第{tc.getMainRoadExtend()}次")
                    # 情境：超過無行人及無行車觸動訊號的延長週期，執行行人最短綠
                    elif (tc.getActuatePedestrianLevel() == 0) and (actuatePedestrianNum == 0) and (actuateCarNum == 0) and (tc.actuateAcceptTime < datetime.datetime.now()) and (mainRoadExtend == extendMaxCycle) and (actuateStatusBool is True):                    
                        tc.actuateAcceptTime = datetime.datetime.now()
                        tc.setActuatePedestrianLevel(1)
                        logging.info(f"**幹道綠燈:1-1延長120秒,到達上限{tc.getMainRoadExtend()}次")
                        rptCycle["rptCycleActuateType"] = "延長上限"
                        rptCycle["triggerType"] = 0
            elif planId in ("21","31") and stepId in ("1-2",):
                    # 情境：幹道綠燈收到行人觸動訊號或前二個週期有行人通過路口
                    if ((actuatePedestrianNum > 0) or (CycleDetectStats["cyPedTotal"] > 0)) and (tc.getActuatePedestrianLevel() == 0) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                        tc.setActuatePedestrianLevel(1)
                        rptCycle["rptCycleActuateType"] = "行人最短綠"
                        rptCycle["triggerType"] = 1
                    # 情境：幹道綠燈只有行車觸動訊號(但無行人觸動)，而且前二個週期有車通過路口
                    elif (tc.getActuatePedestrianLevel() == 0)  and (CycleDetectStats["cyCarTotal"] > 0 or (actuateCarNum > 0)) and (tc.getActuateCarLevel() == 0) and (actuateStatusBool is True):
                        planStepSecondChange(tc.getSeq(), 2, 1, 2)
                        tc.getTimeTable()['2-1'] = 2
                        tc.branchTimeStep['2-1'] = tc.getTimeTable()['2-1']
                        tc.setActuateCarLevel(1)
                        rptCycle["rptCycleActuateType"] = "行車最短綠"
                        rptCycle["triggerType"] = 2
            elif planId in ("21","31") and stepId in ("1-3","1-4"):
                gblSetGreenBranch = False
                gblClearActuateStatus = False
                tc.getTimeTable()['1-1'] = TimeTableSecond[planId]['1-1']                
                rptCycle["rptMainRoadExtend"] = tc.getMainRoadExtend()
                rptCycle["pedgreenflash1"] = tc.getTimeTable()['1-2']
                rptCycle["pedred1"] = tc.getTimeTable()['1-3']
                if rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() == 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18:
                        tc.getTimeTable()['2-1'] = 20
                        tc.branchTimeStep['2-1'] = tc.getTimeTable()['2-1']
                elif rptCycle["rptCycleActuateType"] == "行車最短綠" and tc.getMainRoadExtend() > 0 and  (ext5F1CBool is False):
                    ext5F1CBool = True
                    if tc.getCycleChange("5F1C") != -18 + tc.getMainRoadExtend() * 120:
                        tc.getTimeTable()['2-1'] = 20
                        tc.branchTimeStep['2-1'] = tc.getTimeTable()['2-1']                 
            elif planId in ("21","31") and stepId in ("1-5",):
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['1-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['1-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * tc.getCycleTime() + tc.getTimeTable()['1-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['1-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,1)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=7))
                    # rptCycle["phase1"] = int(rptCycle["phaseEndTime"] - rptCycle["phaseStartTime"])
                    rptCycle["phase1"] = rptSubphase["rptMainStartSecond"] + rptCycle["rptMainRoadExtend"] * 120 + tc.getTimeTable()['1-2'] + tc.getTimeTable()['1-3'] + tc.getTimeTable()['1-4'] + tc.getTimeTable()['1-5']
                if gblClearActuateStatus is False:
                    #統計幹道通行時間行人、行車數量
                    preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                    tc.clearActuate()
                    gblClearActuateStatus = True
            elif planId in ("21","31") and stepId in ("2-1", ):
                gblClearActuateStatus = False                
                if rptSubphase["rptSubphaseStartBool"] is False:
                    rptSubphase = rptSubphaseReset(rptSubphase,'2-1') #開始分相2                    
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now()
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相1的最後時間 
                    tc.intCycleChange() #初始化支道執行秒數延長統計物件
            elif planId in ("21","31") and stepId == "2-2":  # 如果是支道的綠燈步階
                if rptSubphase["rptSubphaseStartBool"] is False:  #Todo:check 正確的步階開始時間
                    rptSubphase = rptSubphaseReset(rptSubphase,'2-1') #開始分相2
                    rptCycle["rptCycleMainRoadEndTime"] = datetime.datetime.now()
                    rptCycle["phaseStartTime"]  = rptCycle["phaseEndTime"]  #接續分相1的最後時間
                if (stepRemainSec >5 ):  # 設定觸動時間，以避免後續一觸動就啟動延長
                    tc.actuateAcceptTime = datetime.datetime.now() 
                if (stepRemainSec >= 3) and (stepRemainSec <= 5):
                    tc.extendMaxSecond = 33 - tc.branchTimeStep['2-1'] - tc.branchTimeStep['2-2'] - tc.branchTimeStep['2-3'] - tc.branchTimeStep['2-4']- tc.branchTimeStep['2-5']
                    if tc.extendMaxSecond >= 3 and (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=3)
                        stepSecond = tc.getTimeTable()['2-2']
                        planStepSecondChange(tc.getSeq(), 2, 2, stepSecond + 3)
                        tc.getTimeTable()['2-2'] = stepSecond + 3
                        tc.branchTimeStep['2-2'] = tc.getTimeTable()['2-2']
                        tc.setBranchExtendNum(1)
                        rptCycle["triggerCar"] +=1
            elif planId in ("21","31") and stepId == "2-3":
                gblClearActuateStatus = False   #2-1可能不執行，所以清空回復值置於此
                tc.extendMaxSecond = 33 - tc.branchTimeStep['2-1'] - tc.branchTimeStep['2-2'] - tc.branchTimeStep['2-3'] - tc.branchTimeStep['2-4']- tc.branchTimeStep['2-5']
                if (tc.extendMaxSecond >= 3) and (stepRemainSec >= 3):  # 尚有餘裕秒數可以延長
                    # 情境：步階中行車觸動訊號延長
                    if (actuateCarOnRoadNum > 0) and (tc.actuateAcceptTime < actuateCarOnRoadTime) and (actuateStatusBool is True):
                        tc.actuateAcceptTime = datetime.datetime.now() + datetime.timedelta(seconds=2)
                        stepSecond = tc.getTimeTable()['2-3']
                        currentStepSecondChange(tc.getSeq(), 2, 3, stepSecond + 3)
                        tc.getTimeTable()['2-3'] = stepSecond + 3
                        tc.branchTimeStep['2-3'] = tc.getTimeTable()['2-3']
                        tc.setBranchExtendNum(1)
                        rptCycle["triggerCar"] +=1
            elif planId in ("21","31") and stepId in ("2-4", "2-5"):
                gblClearActuateStatus = False
                #統計支道時間是否有行人、行車通過路口
                preCycleStay(actuatePedestrianNum+actuatePedestrianOnRoadNum ,actuateCarNum)
                #統計此週期的執行報表
                if rptSubphase["rptSubphaseReportBool"] is False:
                    rptSubphase["rptSubphaseYellow"] = tc.getTimeTable()['2-4']
                    rptSubphase["rptSubphaseRed"] = tc.getTimeTable()['2-5']
                    rptSubphase["rptSubphasePedestrianGreen"] = tc.getTimeTable()['2-1']  + tc.getTimeTable()['2-2']
                    rptSubphase["rptSubphasePedestrianRed"] = tc.getTimeTable()['2-3'] 
                    rptSubphase = rptSubphaseOutput(rptSubphase,2)
                    rptCycle["phaseEndTime"] = datetime.datetime.timestamp(datetime.datetime.now() + datetime.timedelta(seconds=5))
                    rptCycle["pedgreenflash2"] = tc.branchTimeStep['2-2']
                    rptCycle["pedred2"] = tc.branchTimeStep['2-3']
                    calBranchGreenTime = tc.branchTimeStep['2-1'] + tc.branchTimeStep['2-2'] + tc.branchTimeStep['2-3'] + tc.branchTimeStep['2-4']+ tc.branchTimeStep['2-5']
                    extendTotalGreenTime = tc.getCycleChange('5F1C')
                    if rptCycle["rptCycleActuateType"] == "行車最短綠":
                        calBranchGreenTime = 33 - 18 + extendTotalGreenTime                
                    new1_1StepSecond = TimeTableSecond[planId]['1-1'] + (33 - calBranchGreenTime)
                    if (tc.getTimeTable()['1-1'] != new1_1StepSecond) and (actuateStatusBool is True):
                        stepSecond = new1_1StepSecond
                        planStepSecondChange(tc.getSeq(), 1, 1, stepSecond)
                        tc.getTimeTable()['1-1'] = stepSecond
                    rptCycle["rptBranchGreenTime"] = calBranchGreenTime

                if rptCycle["rptCycleReportBool"] is False :
                    rptCycle["phase2"] = calBranchGreenTime                    
                    rptCycle["rptCycleEndTime"] = int(rptCycle["phaseEndTime"])
                    rptCycle["rptCycleLeftGreenSec"] +=  rptCycle["rptMainRoadExtend"] * tc.getCycleTime()
                    rptCycle = rptCycleOutput(rptCycle)
                    #待報表輸出後，再將調撥時間存入
                    rptCycle["rptCycleLeftGreenSec"] = 33- calBranchGreenTime 

            #輸出目前觸動資訊
            logTimeTo = time.time()
            logTimeBase = int(logTimeTo-logTime)
            if logTimeBase > 1:
                logging.info(f'延長:{mainRoadExtend}-{branchExtendNum},行人:{actuatePedestrianNum}-{actuatePedestrianOnRoadNum},行車:{actuateCarNum}-{actuateCarOnRoadNum}')
                logTimeBase =0
                logTime = logTimeTo

            # 更新剩餘秒數
            stepId,stepRemainSec,stepReportTime = tc.getStepParameter()
            periodSecond = stepRemainSec - int((datetime.datetime.now() - stepReportTime).total_seconds())
            tc.setStepParameter(stepId,periodSecond,datetime.datetime.now())

            if periodSecond < 0 :
                if stepId == "1-1":
                    tc.setStepParameter('1-2',tc.getTimeTable()['1-2']+periodSecond,datetime.datetime.now())
                elif stepId == "1-2":
                    tc.setStepParameter('1-3',tc.getTimeTable()['1-3']+periodSecond,datetime.datetime.now())
                elif stepId == "1-3":
                    tc.setStepParameter('1-4',tc.getTimeTable()['1-4']+periodSecond,datetime.datetime.now())
                if stepId == "2-2":
                    tc.setStepParameter('2-3',tc.getTimeTable()['2-3']+periodSecond,datetime.datetime.now())
                elif stepId == "2-3":
                    tc.setStepParameter('2-4',tc.getTimeTable()['2-4']+periodSecond,datetime.datetime.now())
                elif stepId == "3-2":
                    tc.setStepParameter('3-3',tc.getTimeTable()['3-3']+periodSecond,datetime.datetime.now())
                elif stepId == "3-3":
                    tc.setStepParameter('3-4',tc.getTimeTable()['3-4']+periodSecond,datetime.datetime.now())                                    
            event.wait(0.5)
        except KeyboardInterrupt:
            event.set()
            break
        except Exception as e:
            logging.debug("Error")
            logging.debug(e)

    syncTimeThread.join()
    receActiveThread.join()
    receAiThread.join()
    # autoSendThread.join()  
    # centerStartThread.join()

if __name__ == "__main__":
    timer_start = timeit.default_timer()
    main()    
    timer_end = timeit.default_timer()
    logging.info(f'程式完成輸出:{timer_end-timer_start}')
