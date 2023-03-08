import redis
r = redis.Redis(host='192.168.16.66', port=6379, db=1)
r.publish('my_redis_channel','GREETING')

