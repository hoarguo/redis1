import redis

r = redis.Redis(host='192.168.16.66', port=6379, db=1)
r.publish('my_redis_channel','aaa')

sub = r.pubsub()
sub.subscribe('my_redis_channel')
for message in sub.listen():
    print('Got message', message)
    print(type(message))
    # if (
    #     isinstance(message.get('data'), bytes) and
    #     message['data'].decode() == 'GREETING'
    # ):
        # print('Hello')

