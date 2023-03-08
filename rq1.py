from redis import Redis
from rq import Queue
from rq.job import Job

redis = Redis(host='192.168.16.66', port=6379, db=0)
q = Queue('low', connection=redis)
job = Job.create(23,
          ttl=30,  # This ttl will be used by RQ
          args=('http://nvie.com',),)
job = q.enqueue(job, job_id='my_job_id')

