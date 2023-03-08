# -*- coding: utf-8 -*-
import os
import sys
import threading
import queue
import datetime
import time
import timeit

import logging
import redis
import copy
import json
import requests
import re
import serial
import math
import subprocess
import stateClass

# 設定啟動全域參數
gblClearActuateStatus = False  # 是否已經清空觸動數值
gblSetGreenBranch = False  # 是否已經重設支道綠燈秒數

CycleDetectStats = {"cyPedTotal":0,"cyCarTotal":0,
