#!/usr/bin/env python
import os, sys, subprocess, json, argparse, time, shutil
import netifaces
import rethinkdb as r
from common import *

SKIP_DB = '--skip-db-wait'
REGISTRY_PORT = '5000'
WORKER_PORT =   '8080'
BALANCER_PORT = '85'

def container_ip(cid):
    inspect = run_js('docker inspect '+cid)
    return only(inspect)['NetworkSettings']['IPAddress']

def lookup_host_port(cid, guest_port):
    inspect = run_js('docker inspect '+cid)
    return only(only(inspect)['NetworkSettings']['Ports'][str(guest_port)+'/tcp'])['HostPort']

def my_ip(name='eth0'):
    ip = None
    try :
        ip = netifaces.ifaddresses('eth0')[netifaces.AF_INET][0]['addr']
    except:
        pass
    return ip

def write_nginx_config(path, workers):
    config = 'http {\n\tupstream handlers {\n'

    for worker in workers:
	config += '\t\tserver %s:%s;\n' % (worker['ip'], WORKER_PORT)

    config += '\t}\n\tserver {\n\t\tlisten %s;\n\t\tlocation / {\n\t\t\tproxy_pass http://handlers;\n\t\t}\n\t}\n}\nevents{}' % BALANCER_PORT

    with open(path, 'w') as fd:
	fd.write(config)	

def make_cluster_dir():
    global cluster_dir

    cluster_dir = os.path.join(SCRIPT_DIR, args.cluster)

    if os.path.exists(cluster_dir):
        print 'Cluster already running!'
        print 'Use stop-local-cluster.py to clean up.'

    os.mkdir(cluster_dir)

def start_code_reg():
    pass

def start_docker_reg():
    global registry_ip
    global registry_port

    c = 'docker run -d -p 0:%s registry:2' % REGISTRY_PORT
    cid = run(c).strip()

    registry_ip = container_ip(cid)
    registry_port = lookup_host_port(cid, REGISTRY_PORT)

    config_path = os.path.join(cluster_dir, 'registry.json')
    config = {'cid': cid,
              'ip': registry_ip,
              'host_ip': host_ip,
              'host_port': registry_port,
              'type': 'registry'}
    wrjs(config_path, config)

    print 'started registry %s:%s (or localhost:%s)' % (registry_ip, REGISTRY_PORT, registry_port)
    print '='*40

def start_workers():
    global workers

    assert(int(args.workers) > 0)
    workers = []
    for i in range(int(args.workers)):
        config_path = os.path.join(cluster_dir, 'worker-%d.json' % i)
        config = {'registry_host': registry_ip,
                  'registry_port': REGISTRY_PORT,
                  'type': 'worker',
                  'cluster_name': args.cluster}
        if i > 0:
            config['rethinkdb_join'] = workers[0]['ip']+':29015'

        wrjs(config_path, config)
        volumes = [('/sys/fs/cgroup', '/sys/fs/cgroup'),
                   (config_path, '/open-lambda/config.json')]
        c = 'docker run -d --privileged <VOLUMES> -p 0:%s lambda-node' % WORKER_PORT
        c = c.replace('<VOLUMES>', ' '.join(['-v %s:%s'%(host,guest)
                                             for host,guest in volumes]))
        cid = run(c).strip()
        config['cid'] = cid
        config['ip'] = container_ip(cid)
        config['host_ip'] = host_ip
        config['host_port'] = lookup_host_port(cid, WORKER_PORT)
        wrjs(config_path, config, atomic=True)

        print 'started worker %s:%s' % (config['ip'], WORKER_PORT)
        workers.append(config)

    print '='*40

def start_lb():
    global balancer_ip
    global balancer_port

    nginx_path = os.path.join(SCRIPT_DIR, 'nginx.config')
    write_nginx_config(nginx_path, workers)

    c = 'docker run -p 0:%s -v %s:/etc/nginx/nginx.conf:ro -d nginx' % (BALANCER_PORT, nginx_path)
    cid = run(c).strip()

    balancer_ip = container_ip(cid)
    balancer_port = lookup_host_port(cid, BALANCER_PORT)
    config_path = os.path.join(cluster_dir, 'loadbalancer.json')
    config = {'cid': cid,
              'ip': balancer_ip,
              'host_ip': host_ip,
              'host_port': balancer_port,
              'type': 'loadbalancer'}    

    config_path = os.path.join(cluster_dir, 'loadbalancer-%d.json' % 1)
    wrjs(config_path, config, atomic=True)

    print 'started loadbalancer ' + balancer_ip + ':%s' % BALANCER_PORT
    print '='*40

def rethinkdb_wait():
    print 'To continue without waiting for the DB, use ' + SKIP_DB
    for i in range(10):
        try:
            r.connect(workers[0]['ip'], 28015).repl()
            up = len(list(r.db('rethinkdb').table('server_status').run()))
            if up < len(workers):
                print '%d of %d rethinkdb instances are ready' % (up, len(workers))
        except:
            print 'waiting for first rethinkdb instance to come up'

        time.sleep(1)

    print 'all rethinkdb instances are ready'

def print_directions():
    print '='*40
    print 'Push images to OpenLambda registry as follows (or similar):'
    print 'IMG=hello && docker tag $IMG localhost:%s/$IMG; docker push localhost:%s/$IMG' % (registry_port, registry_port)
    print 'OR'
    print ('IMG=hello && docker tag $IMG %s:%s/$IMG; docker push %s:%s/$IMG' %
           (host_ip, registry_port, host_ip, registry_port))
    print '='*40
    print 'Send requests directly to workers as follows (or similar):'
    print "IMG=hello && curl -w \"\\n\" -X POST %s:%s/runLambda/$IMG -d '{}'" % (workers[-1]['ip'], WORKER_PORT)
    print 'OR'
    print "IMG=hello && curl -w \"\\n\" -X POST %s:%s/runLambda/$IMG -d '{}'" % (host_ip, workers[-1]['host_port'])
    print '='*40
    print 'Send requests to the loadbalancer as follows (or similar):'
    print "IMG=hello && curl -w \"\\n\" -X POST %s:%s/runLambda/$IMG -d '{}'" % (workers[-1]['ip'], WORKER_PORT)
    print 'OR'
    print "IMG=hello && curl -w \"\\n\" -X POST %s:%s/runLambda/$IMG -d '{}'" % (balancer_ip, balancer_port)

def main():
    global host_ip
    global args

    host_ip = my_ip()
    if host_ip == None:
        print 'Could not find an IP using netifaces, using 127.0.0.1'
        host_ip = '127.0.0.1'

    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', '-w', default='1')
    parser.add_argument('--cluster', '-c', default='cluster')
    parser.add_argument('--registry', '-r', default='docker')
    parser.add_argument(SKIP_DB, default=False, action='store_true')

    args = parser.parse_args()

    make_cluster_dir()

    if args.registry == "code_reg":
        start_code_reg()
    else:
        start_docker_reg()

    start_workers() 

    start_lb()

    if args.skip_db_wait:
        print "Not waiting for rethinkdb"
    else:
        rethinkdb_wait()

    print_directions()

if __name__ == '__main__':
    main()
