# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2013 Hewlett-Packard Development Company, L.P.
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
"""
usage: qaStressTest.py [-h] [-threads THREADS] [-servers SERVERS]
                       [-volumes VOLUMES] [-logfile LOGFILE]
                       [-keepvm] [-noconfirm]
                       host

positional arguments:
  host              The IP for the openstack controller

optional arguments:
  -h, --help        show this help message and exit
  -threads THREADS  number of worker threads
  -servers SERVERS  number of servers
  -volumes VOLUMES  Number of volumes to create per thread
  -logfile LOGFILE  log file path
  -keepvm           keep the servers
  -noconfirm        do not confirm action before continuing
"""


#import pydevd
#pydevd.settrace('127.0.0.1',suspend=False,stdoutToServer=True,stderrToServer=True)
import threading
#threading.settrace(pydevd.GetGlobalDebugger().trace_dispatch)
import pprint
from random import randint
import time as mytime
from datetime import timedelta as mytimedelta
import traceback

import argparse
from sys import path
import os
import sys

import logging

# import cinder
from cinderclient import exceptions as cinderex
# import cinderclient
from cinderclient.v1.client import Client as cinder
# import novaclient
from novaclient.v1_1.client import Client as nova
# from nova import exception as novaex
from novaclient import exceptions as novaex

ATTACHMENT_LIMIT=26

# wait time in minutes
WAIT_TIME = 5

# wait retry time in seconds
WAIT_RETRY = 60

# confirm wait time in seconds
WAIT_CONFIRM = 300

parser = argparse.ArgumentParser(description="Stress Test Tool")
parser.add_argument("host", type=str,
                     help='The IP for the openstack controller')
parser.add_argument("-threads", dest='threads', type=int,  
                    help="number of worker threads, default is 5", default=5)
parser.add_argument("-servers", dest='servers', type=int,  
                    help="number of servers, default is 5", default=5)
parser.add_argument("-volumes", dest='volumes', type=int,
                    help="Number of volumes to create per thread, "+
                    "default is 5", default=5)
defaultLogfileName = "stressTest-"+mytime.strftime("%Y%m%d-%H%M%S")+".log"
parser.add_argument("-logfile", dest='logfile', 
                    help="log file path, default is "+defaultLogfileName,
                    default=defaultLogfileName)
parser.add_argument("-keepvm",dest="keepvm" ,
                    help="keep the servers, default is False",
                    action="store_true", default=False )
# note that setting True for confirm is to verify that the action was performed, 
# specifying -noconfirm will set to False for no confirmation
parser.add_argument("-noconfirm",dest="confirm",
                    help="do not confirm action before continuing, default is do confirmation",
                    action="store_false", default=True)
args = parser.parse_args()
        
if args.threads * args.volumes > args.servers * ATTACHMENT_LIMIT:
    print("### Too many volumes which cannot all be used to attach to servers")
    print("### Total volumes to create: "+str(args.threads*args.volumes))
    print("### Total volumes that can be used: "+str(args.servers*ATTACHMENT_LIMIT))
    sys.exit(1)

#make sure we have the OS_* environ vars we need
if not os.environ['OS_USERNAME']:
    print "Must have OS_USERNAME environ var set"
    sys.exit(-1)

if not os.environ['OS_PASSWORD']:
    print "Must have OS_PASSWORD environ var set"
    sys.exit(-1)

if not os.environ['OS_TENANT_NAME']:
    print ( "Must have OS_TENANT_NAME environ var set")
    sys.exit(-1)

totalRunActions = 0
totalRunAction_create = 0
totalRunAction_delete = 0
totalRunAction_create_sp = 0
totalRunAction_delete_sp = 0
totalRunAction_attach = 0
totalRunAction_detach = 0

totalRunErrors=0
totalRunError_create = 0
totalRunError_delete = 0
totalRunError_create_sp = 0
totalRunError_delete_sp = 0
totalRunError_attach = 0
totalRunError_detach = 0

auth_url = "http://%s:35357/v2.0" % args.host
 
novacl = nova(os.environ['OS_USERNAME'], 
              os.environ['OS_PASSWORD'],
              os.environ['OS_TENANT_NAME'], 
              auth_url,
              True, None, None,
              None, None,
              'publicURL', None,
              'compute', None,
              None, False,
              None, True,
              False, 'keystone')

cindercl = cinder(os.environ['OS_USERNAME'], 
                  os.environ['OS_PASSWORD'],
                  os.environ['OS_TENANT_NAME'], 
                  auth_url, 
                  True, None, None,
                  None, None, None, 
                  'publicURL', None, 
                  'volume', None, None)

class OpenStackThread ( threading.Thread ):

    #test-<threadid>-<volume-num>
    VOLUME_NAME = "testvol"
    SNAPSHOT_NAME = "testsp"
    SERVERS_NAME = "testserver"

    #static variable
    logger = None
    servers = [] 
    attachCounters = {}

    lockit = threading.Lock()
 
    def __init__(self, host, num_volumes, num_servers, logfile, threadid):
        self.host = host
        self.num_servers = num_servers
        self.num_volumes = num_volumes
        self.num_snapshots = num_volumes
        self.threadid=threadid

        threading.Thread.__init__ ( self )

        self.auth_url = "http://%s:35357/v2.0" % self.host
        
        if OpenStackThread.logger == None:
            OpenStackThread.setup_logging(logfile)

        self.clock_start = mytime.time()
        
        self.cindercl=self._create_cinder_client()
        self.novacl=self._create_nova_client()

        self.volumes = []
        self.snapshots = []


        self.total_actions = 0
        self.action_create = 0
        self.action_delete = 0
        self.action_create_sp = 0
        self.action_delete_sp = 0
        self.action_attach = 0
        self.action_detach = 0
        self.total_errors = 0
        self.error_delete = 0
        self.error_create = 0
        self.error_create_sp = 0
        self.error_delete_sp = 0
        self.error_attach = 0
        self.error_detach = 0
        
       
        if len (OpenStackThread.servers) == 0:
            if not self.get_existing_servers():
                self.create_servers()

    @staticmethod
    def setup_logging(logfile=None):
        # create logger
        OpenStackThread.logger = logging.getLogger()
        OpenStackThread.logger.setLevel(logging.INFO)
        # create formatter
        formatter = logging.Formatter('%(asctime)s%(message)s',
                                      datefmt="[%Y-%m-%d][%H:%M:%S]")
        
        # create console handler and set level
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)

        # add formatter to ch
        ch.setFormatter(formatter)

        # add ch to logger
        OpenStackThread.logger.addHandler(ch)

        # also log to a file given on command line
        if logfile:
            ch = logging.FileHandler(logfile,"w")
            ch.setLevel(logging.INFO)
            ch.setFormatter(formatter)
            OpenStackThread.logger.addHandler(ch)
            
        # turn off "Starting new HTTP connection" message
        urllib3_logger = logging.getLogger('urllib3')
        urllib3_logger.setLevel(logging.WARNING) 

    @staticmethod
    def log_message(msg):
        OpenStackThread.logger.info(msg)
            

    def _log_message(self, msg="", actionIncrement=0, action=None):
        self.total_actions += actionIncrement

        entry = "[A:%d,E:%d] %s" % (self.total_actions,
                                    self.total_errors, msg)
        OpenStackThread.logger.info(entry)


        if action and action == "create_volume":
            self.action_create += actionIncrement
        elif action and action == "delete_volume":
            self.action_delete += actionIncrement
        elif action and action == "create_snapshot":
            self.action_create_sp += actionIncrement
        elif action and action == "delete_snapshot":
            self.action_delete_sp += actionIncrement
        elif action and action == "attach_volume":
            self.action_attach += actionIncrement
        elif action and action == "detach_volume":
            self.action_detach += actionIncrement

      
    def _log_error(self, msg="", errorIncrement=0, action=None,
                   exception=None):
        self.total_errors += errorIncrement

        entry = "[A:%d,E:%d] ### %s" % (self.total_actions,
                                    self.total_errors, msg)
        OpenStackThread.logger.error(entry)

        if action and action == "create_volume":
            self.error_create += errorIncrement
        elif action and action == "delete_volume":
            self.error_delete += errorIncrement
        elif action and action == "create_snapshot":
            self.error_create_sp += errorIncrement
        elif action and action == "delete_snapshot":
            self.error_delete_sp += errorIncrement
        elif action and action == "attach_volume":
            self.error_attach += errorIncrement
        elif action and action == "detach_volume":
            self.error_detach += errorIncrement

        if exception:
            OpenStackThread.logger.exception(exception)

    def _create_nova_client(self):
        novacl = nova(os.environ['OS_USERNAME'], 
                      os.environ['OS_PASSWORD'],
                      os.environ['OS_TENANT_NAME'], 
                      self.auth_url,
                      True, None, None,
                      None, None,
                      'publicURL', None,
                      'compute', None,
                       None, False,
                       None, True,
                       False, 'keystone')

        return novacl

    def _create_cinder_client(self):
        cindercl = cinder(os.environ['OS_USERNAME'],
                          os.environ['OS_PASSWORD'],
                          os.environ['OS_TENANT_NAME'],
                          self.auth_url, True, None, None,
                          None, None, None,
                          'publicURL', None, 'volume', None, None)
        return cindercl

    def get_volumes(self):
        try:
            self.volumes = self.cindercl.volumes.list(True)
        except:
            self._log_error("Thread(%s) - %s" % (self.threadid, traceback.format_exc()))
            self._log_error("Thread(%s) - get_volumes failed", (self.threadid))


    def show_volumes(self):
        print "Show Volumes"
        list_ = self.cindercl.volumes.list(True)
        for volume in list_:
            pprint.pprint(volume)

    def _confirm_create_volume(self, volume):        
        # confirm that volume was created
        if args.confirm == True:
            self._log_message("Thread(%s) - confirming creation of volume %s" % (self.threadid, volume.id))
            w_time = mytime.time()
            volStatus = self.cindercl.volumes.get(volume.id).status
            while mytime.time() - w_time < WAIT_CONFIRM:
                if volStatus == "available":
                    self._log_message("Thread(%s) - confirmed creation of volume %s after %s seconds" % (self.threadid, volume.id, str(mytime.time() - w_time)))
                    return
                mytime.sleep(5)
                volStatus = self.cindercl.volumes.get(volume.id).status
            if volStatus != "available":
                self._log_error("Thread(%s) - Unable to confirm creation of volume %s after %s seconds" % (self.threadid, volume.id, str(mytime.time() - w_time)), 1, "create-snapshot")


    def create_volumes(self):
        self._log_message( "Thread(%s) - Will create %s volumes" %
                           (self.threadid, self.num_volumes))

        try: 
            a = 0
            vol = None
            while a < self.num_volumes:
                vol_name =  OpenStackThread.VOLUME_NAME + "-" + str(self.threadid) + "-" + str(a)
                vol_desc = "Created by qaStressTest thread-"+str(self.threadid)
                vol_size = randint(1, 5)
                try: 
                    vol = self.cindercl.volumes.create(vol_size, None,
                                                  vol_name,
                                                  vol_desc,
                                                  None, None,
                                                  None, None, None, None)
                    self._log_message ("Thread(%s)a - Creating volume(%s)  %s " %
                                       (self.threadid, a, vol.id), 1,
                                       "create_volume")
                    self.volumes.append(vol)
            
                    self._confirm_create_volume(vol)
                    a = a + 1
                    
                except cinderex.NotFound:
                    # except cinderex.VolumeNotFound: deal with new sig for
                    # the method...VolumeNotFound doesn't work
                    vol = self.cindercl.volumes.create(vol_size, None, None,
                                                  vol_name, vol_desc,
                                                  None, None,
                                                  None, None, None, None)
                    self._log_message ("Thread(%s)b - Created volume(%s)  %s " %
                                       (self.threadid, a, vol.id), 1, "create_volume")
                    self.volumes.append(vol)
                    self._confirm_create_volume(vol)
                    a = a + 1
        except cinderex.OverLimit as ex:
            self._log_error( "Thread(%s) - Volume Quota reached. Giving up on volume(%s)" %
                             (self.threadid, a), 1, "create_volume",ex)
            pass 
        except Exception as ex:
            #pprint.pprint(ex)
            self._log_error( "Thread(%s) - Create Volume failed. Giving up on voume(%s)" %
                             (self.threadid,a), 1, "create_volume",ex)
            raise ex

    def _confirm_delete_volume(self, volume):
        # confirm that volume was deleted
        if args.confirm == True:
            try:
                self._log_message("Thread(%s) - confirming deletion of volume %s" % (self.threadid, volume.id))
                w_time = mytime.time()
                volStatus = self.cindercl.volumes.get(volume.id).status
                while mytime.time() - w_time < WAIT_CONFIRM:
                    if volStatus == "deleted":
                        self._log_message("Thread(%s) - confirmed deletion of volume %s after %s seconds" % (self.threadid, volume.id, str(mytime.time() - w_time)))
                        return
                    mytime.sleep(5)
                    volStatus = self.cindercl.volumes.get(volume.id).status
                if volStatus != "deleted":
                    self._log_error("Thread(%s) - Unable to confirm deletion of volume %s after %s seconds" % (self.threadid, volume.id, str(mytime.time() - w_time)), 1, "delete_volume")
            except cinderex.NotFound:
                    self._log_message("Thread(%s) - confirmed deletion of volume %s after %s seconds" % (self.threadid, volume.id, str(mytime.time() - w_time)))
            except:
                self._log_error("Thread(%s)a %s" % (self.threadid,traceback.format_exc()))
                self._log_error("Thread(%s)a - failed to confirm deletion of volume %s - %s, status %s " % (self.threadid, volume.id, volume.display_name, volStatus), 1, "delete_volume")


    def _delete_volume(self, volume):
        #get latest and update
        volume = self.cindercl.volumes.get(volume.id)
        if volume.status == 'available' or volume.status == "error":
            try:
                self.cindercl.volumes.delete(volume)
                self._log_message( "Thread(%s)a - deleting volume %s" % (self.threadid, volume.id),1, "delete_volume")
                self._confirm_delete_volume(volume)
            except:
                self._log_error("Thread(%s)a %s" % (self.threadid,traceback.format_exc()))
                self._log_error("Thread(%s)a - failed to delete volume %s - %s, status %s " % (self.threadid, volume.id, volume.display_name, volume.status), 1, "delete_volume")
        else:
            # if volume status is in-use state, try a detach again, just in case request got lost
            if volume.status == "in-use":
                self._log_message ("Thread(%s) - trying to delete volume %s with attachment, will try detach again" % (self.threadid,volume.id))
                self._detach_volumes(volume)
                
            #have a little bit wait, just in case
            w_time = mytime.time()
            self._log_message( "Thread(%s) - trying to delete volume %s  with status %s, so sleep" % (self.threadid, volume.id, volume.status))
            while ((volume.status == 'creating' or  volume.status == "in-use" or volume.status == 'detaching') and (mytime.time()  - w_time < WAIT_TIME * 60)):
                mytime.sleep(5)
                volume = self.cindercl.volumes.get(volume.id)
            #last try  
            if volume.status == 'available' or volume.status == "error":
                try: 
                    self.cindercl.volumes.delete(volume)
                    self._log_message( "Thread(%s)b - deleting volume %s" % (self.threadid, volume.id),1, "delete_volume")
                    self._confirm_delete_volume(volume)
                except:
                    self._log_error("Thread(%s)b %s" % (self.threadid,traceback.format_exc()))
                    self._log_error("Thread(%s)b - failed to delete volume %s - %s, status %s" % (self.threadid, volume.id, volume.display_name, volume.status), 1, "delete_volume")
            else:
                self._log_error("Thread(%s) - volume %s status is %s, not in state for deletion" % (self.threadid, volume.id, volume.status), 1, "delete_volume")
            

    def delete_volumes(self):
        self._log_message( "Thread(%s) - Will delete %s volumes" % (self.threadid, len(self.volumes)))

        for volume in self.volumes:
            if not self._has_dep(volume):
                self._delete_volume(volume)
            else:
                w_time = mytime.time()
                self._log_message( "Thread(%s) - trying to delete volume %s with snapshot , so sleep" % (self.threadid, volume.id))
                while ( self._has_dep(volume) and (mytime.time() - w_time < WAIT_TIME * 60 )):
                    mytime.sleep(5)
                if self._has_dep(volume):
                    self._log_error( "Thread(%s) - volume %s has snapshot , will skip" % (self.threadid, volume.id), 1, "delete_volume")
                else: 
                    self._delete_volume(volume)

        self.volumes = []

    def _attach_volumes(self, volume):
        server = OpenStackThread.servers[randint(0, self.num_servers-1)]
        
        # if chosen server has met attachment limit, find next available server
        if OpenStackThread.attachCounters[server.id] >= ATTACHMENT_LIMIT:
            for item in OpenStackThread.servers:
                if OpenStackThread.attachCounters[item.id] < ATTACHMENT_LIMIT:
                    OpenStackThread.attachCounters[item.id]+=1
                    server = item
                    break
        else:
            OpenStackThread.attachCounters[server.id]+=1
            
        # generate device name to use
        deviceName = ""
        num = OpenStackThread.attachCounters[server.id]
        while num >= 26:
            q = num / 26
            deviceName = chr (97+q) + deviceName
            num = num - 26 * q
        deviceName = "/dev/vd" + deviceName + chr (97+num)

            
        while True:
            try:
                self._log_message("Thread(%s) -  trying to attach volume %s to server %s using %s" % (self.threadid,volume.id, server.id, deviceName), 0, "attach_volume")
                self.novacl.volumes.create_server_volume(server.id, volume.id, deviceName)
                self._log_message("Thread(%s) -  attach request submitted -- attach volume %s to server %s using %s" % (self.threadid,volume.id, server.id, deviceName), 1, "attach_volume")
                break
            except novaex.OverLimit:
                # wait and then retry
                self._log_message("Thread(%s) -  attaching volume %s to server %s using %s failed, overlimit; will sleep and retry" % (self.threadid,volume.id, server.id, deviceName), 0, "attach_volume")
                mytime.sleep(WAIT_RETRY)
                continue
            except:
                self._log_error("Thread(%s) %s" % (self.threadid,traceback.format_exc()))
                self._log_error("Thread(%s) -  cannot attach volume %s to server %s using %s, will skip " % (self.threadid,volume.id, server.id, deviceName), 1, "attach_volume")
                return
            
        # if confirmation option is selected, wait for attachment before continuing
        if args.confirm == True:
            self._log_message("Thread(%s) - confirming attachment of volume %s to server %s using %s" % (self.threadid, volume.id, server.id, deviceName))
            w_time = mytime.time()
            volStatus = self.cindercl.volumes.get(volume.id).status
            while mytime.time() - w_time < WAIT_CONFIRM:
                if volStatus == "in-use":
                    self._log_message("Thread(%s) - confirmed attachment of volume %s to server %s using %s after %s seconds" % (self.threadid, volume.id, server.id, deviceName, str(mytime.time() - w_time)))
                    return
                mytime.sleep(5)
                volStatus = self.cindercl.volumes.get(volume.id).status
            
            self._log_error("Thread(%s) - unable to confirm attachment of volume %s to server %s using %s after %s seconds, status is %s" % (self.threadid, volume.id, server.id, deviceName, str(mytime.time() - w_time), volStatus), 1, "attach_volume")
    


    def attach_volumes(self):
        self._log_message( "Thread(%s) - will attach %s volumes" % (self.threadid, len(self.volumes)))

        if not OpenStackThread.servers or len(OpenStackThread.servers) < self.num_servers:
            self._log_error( "Thread(%s) - cannot attach volumes since not enough servers "% (self.threadid), 1, "attach_volume")
            raise

        for volume in self.volumes:
            #get latest status and update 
            volume = self.cindercl.volumes.get(volume.id)
            if volume.status == "available":
                self._attach_volumes(volume)
            else:
                self._log_message("Thread(%s) - trying to attach volume %s not in available state, need to wait" % (self.threadid, volume.id))
                w_wait = mytime.time() 
                while volume.status != "available" and mytime.time() - w_wait < WAIT_TIME * 60:
                    mytime.sleep(10)
                    volume = self.cindercl.volumes.get(volume.id)
                    
                if volume.status == "available": 
                    self._attach_volumes(volume)
                else: 
                    self._log_error("Thread(%s) - cannot attach volume %s which is not avaiable after sleep, will skip " % (self.threadid,volume.id), 1, "attach_volume")

    def _detach_volumes(self, volume):
        serverId = volume.attachments[0]['server_id']
        while True:
            try:
                self._log_message( "Thread(%s) -  trying to detach volume %s from server %s" % (self.threadid,volume.id,volume.attachments[0]['server_id']), 0, "detach_volume")
                self.novacl.volumes.delete_server_volume(serverId, volume.id)
                self._log_message( "Thread(%s) -  detach requested submitted -- detach volume %s from server %s" % (self.threadid,volume.id,serverId), 1, "detach_volume")
                break
            except novaex.OverLimit:
                # wait and then retry
                self._log_message("Thread(%s) -  detaching volume %s from server %s failed, overlimit; will sleep and retry" % (self.threadid,volume.id, volume.attachments[0]['server_id']), 0, "detach_volume")
                mytime.sleep(WAIT_RETRY)
                continue
            except:
                self._log_error("Thread(%s) %s" % (self.threadid,traceback.format_exc()))
                self._log_error("Thread(%s) - cannot detach volume %s, will skip " % (self.threadid,volume.id), 1, "detach_volume")
                return
            
        # if confirmation option is selected, wait for detachment before continuing
        if args.confirm == True:
            self._log_message("Thread(%s) - confirming detachment of volume %s from server %s" % (self.threadid,volume.id, serverId))
            w_time = mytime.time()
            volStatus = self.cindercl.volumes.get(volume.id).status
            while mytime.time() - w_time < WAIT_CONFIRM:
                if volStatus == "available":
                    self._log_message("Thread(%s) - confirmed detachment of volume %s from server %s after %s seconds" % (self.threadid,volume.id, serverId, str(mytime.time() - w_time)))
                    return
                mytime.sleep(5)
                volStatus = self.cindercl.volumes.get(volume.id).status
                
            self._log_error("Thread(%s) - unable to confirm detachment of volume %s from server %s after %s seconds, status is %s" % (self.threadid,volume.id, serverId, str(mytime.time() - w_time), volStatus), 1, "detach_volume")

    def detach_volumes(self):
        self._log_message( "Thread(%s) - will detach %s volumes" % (self.threadid, len(self.volumes)))

        for volume in self.volumes:
            #get latest status and update 
            volume = self.cindercl.volumes.get(volume.id)
            if volume.status == "in-use":
                self._detach_volumes(volume)
            elif volume.status == "attaching":
                self._log_message("Thread(%s) - trying to detach volume %s in attaching state, need to wait to go to in-use state" % (self.threadid, volume.id))
                w_wait = mytime.time() 
                while volume.status == "attaching" and mytime.time() - w_wait < WAIT_TIME * 60:
                    mytime.sleep(10)
                    volume = self.cindercl.volumes.get(volume.id)
                    
                if volume.status == "in-use":
                    self._detach_volumes(volume)
                        
                else:
                    if volume.status == "available":
                        # if in attaching state and then become available, then the attach never did happen
                        self._log_error("Thread(%s)a - detach for an attach that never happened for volume %s, status %s" % (self.threadid, volume.id, volume.status), 1, "detach_volume")
                    else:
                        self._log_error("Thread(%s) - cannot detach volume %s after sleep, volume status is %s, will skip " % (self.threadid,volume.id, volume.status), 1, "detach_volume")
            else:
                self._log_error("Thread(%s)b - detach for an attach that never happened for volume %s, status %s" % (self.threadid, volume.id, volume.status), 1, "detach_volume")


    def _has_dep (self, volume):
 
        snapshots = self.get_snapshots()
        for sp in snapshots:
            try:
                #get a fresh one
                sp = self.cindercl.volume_snapshots.get(sp.id)
                if sp.volume_id == volume.id:
                    return True
            except:
                pass
            
        return False
    
    
    """
    def show_dep (self, info):
       cindercl = self._create_cinder_client()
       snapshots = self.get_snapshots(info)
       volumes = self.get_volumes(info)

       for volume in volumes:
           if not self._has_dep (volume, info):
              print "volume %s - %s has no dep" % (volume.id,volume.display_name)
           else: 
              print "volume %s - %s has dep" % (volume.id,volume.display_name)
    """

    def get_snapshots(self):
        
        return self.cindercl.volume_snapshots.list(True)

    def _confirm_create_snapshot(self, snapshot):        
        # confirm that snapshot was created
        if args.confirm == True:
            self._log_message("Thread(%s) - confirming creation of snapshot %s" % (self.threadid, snapshot.id))
            w_time = mytime.time()
            volStatus = self.cindercl.volume_snapshots.get(snapshot.id).status
            while mytime.time() - w_time < WAIT_CONFIRM:
                if volStatus == "available":
                    self._log_message("Thread(%s) - confirmed creation of snapshot %s after %s seconds" % (self.threadid, snapshot.id, str(mytime.time() - w_time)))
                    return
                mytime.sleep(5)
                volStatus = self.cindercl.volume_snapshots.get(snapshot.id).status
            if volStatus != "available":
                self._log_error("Thread(%s) - Unable to confirm creation of snapshot %s after %s seconds" % (self.threadid, snapshot.id, str(mytime.time() - w_time)), 1, "create_snapshot")


    def create_snapshots(self):
        self._log_message( "Thread(%s) - Will create %s snapshots" % (self.threadid, self.num_snapshots))

        sp = None
        try:
            for volume in self.volumes:
                sp_name = OpenStackThread.SNAPSHOT_NAME + "-" + str(self.threadid) + "-" + volume.display_name
                sp_desc = "Created by qaStessTest thread-" + str(self.threadid)
                #get updaed status
                volume = self.cindercl.volumes.get(volume.id)
                if volume.status == 'available':
                    try:
                        sp = self.cindercl.volume_snapshots.create(volume.id, False, sp_name, sp_desc)
                        self._log_message( "Thread(%s)a - creating snapshot for volume %s " % (self.threadid,volume.id), 1, "create_snapshot")
                        self.snapshots.append(sp)
                        self._confirm_create_snapshot(sp)
                    except:
                        self._log_error("Thread(%s)a - %s" % (self.threadid, traceback.format_exc()))
                        self._log_error("Thread(%s)a - cannot create snapshot for volume %s" % (self.threadid, volume.id), 1, "create_snapshot")
                elif volume.status == 'creating' :
                    w_time = mytime.time()
                    self._log_message( "Thread(%s) - creating snapshot for volume %s in creating , so sleep" % (self.threadid, volume.id))
                    while (volume.status == "creating" and  (mytime.time() - w_time < WAIT_TIME * 60)):
                        mytime.sleep(5)
                        volume = self.cindercl.volumes.get(volume.id)
                    if volume.status == 'available' or volume.status == "in-use":
                        try: 
                            sp = self.cindercl.volume_snapshots.create(volume.id, False, sp_name, sp_desc)
                            self._log_message( "Thread(%s)b - Created snapshot for volume  %s " % (self.threadid,  volume.id), 1, "create_snapshot")
                            self.snapshots.append(sp)
                            self._confirm_create_snapshot(sp)
                        except:
                            self._log_error("Thread(%s)b - %s" % (self.threadid, traceback.format_exc()))
                            self._log_error("Thread(%s)b - cannot create snapshot for volume %s" % (self.threadid, volume.id), 1, "create_snapshot")
                    else:
                        self._log_error( "Thread(%s) - creating snapshot for volume %s which is not available after sleep, will skip" % (self.threadid,  volume.id), 1, "create_snapshot")
                                
        except Exception as ex:
            self._log_error( "Thread(%s) - failed to create snapshot for volume " % (self.threadid), 1, "create_snapshot", ex)
            pass

    def _confirm_delete_snapshot(self, snapshot):
        # confirm that volume was deleted
        if args.confirm == True:
            try:
                self._log_message("Thread(%s) - confirming deletion of snapshot %s" % (self.threadid, snapshot.id))
                w_time = mytime.time()
                volStatus = self.cindercl.volume_snapshots.get(snapshot.id).status
                while mytime.time() - w_time < WAIT_CONFIRM:
                    if volStatus == "deleted":
                        self._log_message("Thread(%s) - confirmed deletion of snapshot %s after %s seconds" % (self.threadid, snapshot.id, str(mytime.time() - w_time)))
                        return
                    mytime.sleep(5)
                    volStatus = self.cindercl.volume_snapshots.get(snapshot.id).status
                if volStatus != "deleted":
                    self._log_error("Thread(%s) - Unable to confirm deletion of snapshot %s after %s seconds" % (self.threadid, snapshot.id, str(mytime.time() - w_time)), 1, "delete_volume")
            except cinderex.NotFound:
                    self._log_message("Thread(%s) - confirmed deletion of snapshot %s after %s seconds" % (self.threadid, snapshot.id, str(mytime.time() - w_time)))
            except:
                self._log_error("Thread(%s)a %s" % (self.threadid,traceback.format_exc()))
                self._log_error("Thread(%s)a - failed to confirm deletion of snapshot %s, status %s " % (self.threadid, snapshot.id, volStatus), 1, "delete_snapshot")


    def _delete_snapshot(self, snapshot):
        snapshot = self.cindercl.volume_snapshots.get(snapshot.id)
        if snapshot.status == 'available' or snapshot.status == "error":
            try:
                self.cindercl.volume_snapshots.delete(snapshot)
                self._log_message( "Thread(%s)a - deleting snapshot %s - %s " % (self.threadid, snapshot.id, snapshot.display_name),1,"delete_snapshot")
                self._confirm_delete_snapshot(snapshot)
            except Exception as ex:
                self._log_error( "Thread(%s)a - failed to delete snapshot %s - %s" % (self.threadid, snapshot.id, snapshot.display_name), 1, "delete_snapshot",ex)
                raise ex
        elif snapshot.status == 'creating':
            self._log_message( "Thread(%s) - deleting snapshot %s in creating...so sleep" % (self.threadid, snapshot.id))
            w_time = mytime.time()
            while (snapshot.status == "creating" and  (mytime.time() - w_time < WAIT_TIME * 60)):
                mytime.sleep(5)
                snapshot = self.cindercl.volume_snapshots.get(snapshot.id)
            if snapshot.status == 'available' or snapshot.status == "error":
                try: 
                    self.cindercl.volume_snapshots.delete(snapshot)
                    self._log_message( "Thread(%s)b - deleted snapshot %s - %s " % (self.threadid, snapshot.id, snapshot.display_name),1,"delete_snapshot")
                    self._confirm_delete_snapshot(snapshot)
                except Exception as ex:
                    self._log_error( "Thread(%s)b - failed to delete snapshot %s - %s" % (self.threadid, snapshot.id, snapshot.display_name), 1, "delete_snapshot",ex)
                    raise ex
            else: 
                self._log_error( "Thread(%s) - trying to delete snapshot %s which is still in creating after sleep...so skip- %s" % (self.threadid, snapshot.id, snapshot.display_name), 1, "delete_snapshot")

    def delete_snapshots(self):
        self._log_message( "Thread(%s) - Will delete %s snapshots" % (self.threadid, len(self.snapshots)))

        for sp in self.snapshots:
            self._delete_snapshot(sp)

        self.snapshots = []


    def _test_server(self, server):
        """
        Check the server status and current task state
        if status is Active and task is None then it is available
        """
        server = self.novacl.servers.get(server.id)
        task = server._info.get("OS-EXT-STS:task_state")
        status = server._info.get("status")

        self._log_message( "Testing server %s - %s, %s" % (server.name, task, status))
        
        if (task == None and status == "ACTIVE"):
            return True
        return False
        

    def create_servers(self):
        
        # determine the number of additional servers needed
        need = self.num_servers - len (OpenStackThread.servers)
        if need <= 0:
            return
        
        self._log_message( "Create  %s VirtualMachines" % need)
        
        # get list of images and use the first one in the list
        images = self.novacl.images.list()
        if images and len(images) > 0:
            image = images[0]

        # get list of flavors and use the tiny one
        flavors = self.novacl.flavors.list()
        tinyFlavor = None
        for flav in flavors:
            if flav.name  == "m1.tiny":
                tinyFlavor = flav
                break

        #  create the needed servers to run the test
        for i in range(need):
            server = self.novacl.servers.create(OpenStackThread.SERVERS_NAME+mytime.strftime("-%Y%m%d-%H%M%S-")+str(i), 
                                                image, tinyFlavor,
                                                None, None, None, 1)
            self._log_message("Waiting for server %s with ID %s to get spawned" % (server.name,server.id))
        
            while True:
                if self._test_server(server):
                    OpenStackThread.servers.append(server)
                    OpenStackThread.attachCounters[server.id]=0
                    break
                mytime.sleep(5)
                


    def get_existing_servers(self):
        servers = self.novacl.servers.list()

        ctr = 0
        for server in servers:
            if OpenStackThread.SERVERS_NAME in server.name:
                if self._test_server(server):
                    OpenStackThread.servers.append(server)
                    OpenStackThread.attachCounters[server.id]=0
                    ctr += 1
                    if ctr >= self.num_servers:
                        return True
                    
        return False
                


    def run (self):
#        pydevd.settrace('127.0.0.1',suspend=True,stdoutToServer=True,stderrToServer=True)
        
        #print "run called %s" % str(thread)
        self._log_message( "Thread(%s) - Test started " % (self.threadid))

        self.create_volumes()

#        mytime.sleep(randint(5, 10))
        
        self.create_snapshots()

#        mytime.sleep(randint(5, 10))

        self.attach_volumes()
        
        self._log_message( "Thread(%s) - Sleeping for 60 seconds before start detach" % (self.threadid))
        mytime.sleep(60)

#        mytime.sleep(randint(5, 10))

        self.detach_volumes()

#        mytime.sleep(randint(5, 10))

        self.delete_snapshots()

#        mytime.sleep(randint(5, 10))

        self.delete_volumes()
        #self.show_dep()

    def test_finished(self):

        global totalRunActions
        global totalRunAction_create
        global totalRunAction_delete
        global totalRunAction_create_sp
        global totalRunAction_delete_sp
        global totalRunAction_attach
        global totalRunAction_detach
        
        global totalRunErrors
        global totalRunError_create
        global totalRunError_delete
        global totalRunError_create_sp
        global totalRunError_delete_sp
        global totalRunError_attach
        global totalRunError_detach
        
        OpenStackThread.lockit.acquire()
        totalRunActions += self.total_actions
        totalRunAction_create += self.action_create
        totalRunAction_delete += self.action_delete
        totalRunAction_create_sp += self.action_create_sp
        totalRunAction_delete_sp += self.action_delete_sp
        totalRunAction_attach += self.action_attach
        totalRunAction_detach += self.action_detach
        
        totalRunErrors += self.total_errors
        totalRunError_create += self.error_create
        totalRunError_delete += self.error_delete
        totalRunError_create_sp += self.error_create_sp
        totalRunError_delete_sp += self.error_delete_sp
        totalRunError_attach += self.error_attach
        totalRunError_detach += self.error_detach
        OpenStackThread.lockit.release()
        
        self._log_message("Thread(%s) - test performed %s actions." % (self.threadid, self.total_actions))
        self._log_message("Thread(%s) - test performed %s create volume actions." % (self.threadid, self.action_create))
        self._log_message("Thread(%s) - test performed %s delete volume actions." % (self.threadid, self.action_delete))
        self._log_message("Thread(%s) - test performed %s create snapshot actions." % (self.threadid, self.action_create_sp))
        self._log_message("Thread(%s) - test performed %s delete snapshot actions." % (self.threadid, self.action_delete_sp))
        self._log_message("Thread(%s) - test performed %s attach volume actions." % (self.threadid, self.action_attach))
        self._log_message("Thread(%s) - test performed %s detach volume actions." % (self.threadid, self.action_detach))

        self._log_message("Thread(%s) - test observed %s errors" % (self.threadid, self.total_errors))
        self._log_message("Thread(%s) - test observed %s create volume errors" % (self.threadid, self.error_create))
        self._log_message("Thread(%s) - test observed %s delete volume errors" % (self.threadid, self.error_delete))
        self._log_message("Thread(%s) - test observed %s create snapshot errors" % (self.threadid, self.error_create_sp))
        self._log_message("Thread(%s) - test observed %s delete snapshot errors" % (self.threadid, self.error_delete_sp))
        self._log_message("Thread(%s) - test observed %s attach volume errors" % (self.threadid, self.error_attach))
        self._log_message("Thread(%s) - test observed %s detach volume errors" % (self.threadid, self.error_detach))

        self._log_message("Thread(%s) - test test time: %s " % (self.threadid, mytimedelta(seconds=mytime.time()-self.clock_start)))
        self._log_message("Thread(%s) - test finished." % (self.threadid))

def _finish_delete_snapshot (sp):
    while True:
        try:
            OpenStackThread.log_message("Trying to delete snapshot %s" % (sp.id))
            cindercl.volume_snapshots.delete(sp)
            OpenStackThread.log_message("Deleting snapshot %s" % (sp.id))
            
            # confirm that the deletion occurred
            if args.confirm == True:
                OpenStackThread.log_message("Confirming deletion of snapshot %s" % (sp.id))
                w_time = mytime.time()
                spStatus = cindercl.volume_snapshots.get(sp.id).status
                while mytime.time() - w_time < WAIT_CONFIRM:
                    if spStatus == "deleted":
                        OpenStackThread.log_message("Confirmed deletion of snapshot % after % seconds" % (sp.id, str(mytime.time() - w_time)))
                        return
                    mytime.sleep(5)
                    spStatus = cindercl.volume_snapshots.get(sp.id).status
                OpenStackThread.log_message("Unable to confirm deletion of snapshot %s after % seconds, status %s" % (sp.id, str(mytime.time() - w_time), spStatus))
            return

        except cinderex.NotFound:
            OpenStackThread.log_message("Confirmed deletion of snapshot % after % seconds" % (sp.id, str(mytime.time() - w_time)))
            return
            
        except cinderex.OverLimit:
            # wait and then retry
            OpenStackThread.log_message("Deleting snapshot %s failed, overlimit; will sleep and retry" % (sp.id))
            mytime.sleep(WAIT_RETRY)
            continue
        
        except:
            OpenStackThread.log_message ("### Deleting snapshot exception")
            OpenStackThread.log_message ("### sp: %s" % (str(sp)))
            OpenStackThread.log_message (traceback.format_exc())
            return


def _finish_delete_volume (vol):
    while True:
        try:
            OpenStackThread.log_message("Trying to delete volume %s" % (vol.id))
            cindercl.volumes.delete(vol)
            OpenStackThread.log_message("Deleted volume %s" % (vol.id))
            
            # confirm that the deletion occurred
            if args.confirm == True:
                OpenStackThread.log_message("Confirming deletion of volume %s" % (vol.id))
                w_time = mytime.time()
                volStatus = cindercl.volumes.get(vol.id).status
                while mytime.time() - w_time < WAIT_CONFIRM:
                    if volStatus == "deleted":
                        OpenStackThread.log_message("Confirmed deletion of volume % after % seconds" % (vol.id, str(mytime.time() - w_time)))
                        return
                    mytime.sleep(5)
                    volStatus = cindercl.volumes.get(vol.id).status
                OpenStackThread.log_message("Unable to confirm deletion of volume %s after % seconds, status %s" % (vol.id, str(mytime.time() - w_time), volStatus))
            return

        except cinderex.NotFound:
            OpenStackThread.log_message("Confirmed deletion of volume % after % seconds" % (vol.id, str(mytime.time() - w_time)))
            return
            
        except cinderex.OverLimit:
            # wait and then retry
            OpenStackThread.log_message("Deleting volume %s failed, overlimit; will sleep and retry" % (vol.id))
            mytime.sleep(WAIT_RETRY)
            continue
        
        except:
            OpenStackThread.log_message ("### Deleting volume exception")
            OpenStackThread.log_message ("### vol: %s" % (str(vol)))
            OpenStackThread.log_message (traceback.format_exc())
            return


#keep track of threads so we can do clean up
threads = []
for x in xrange( args.threads ):

    ost = OpenStackThread(args.host, args.volumes, args.servers, args.logfile, x)
    
    # creating the first thread will create the logger so we can use now
    if len(threads) == 0:
        OpenStackThread.log_message("Number of threads: "+str(args.threads))
        OpenStackThread.log_message("Number of servers: "+str(args.servers))
        OpenStackThread.log_message("Number of volumes: "+str(args.volumes))
        OpenStackThread.log_message("Log file name: "+str(args.logfile))
        OpenStackThread.log_message("Controller host: "+str(args.host))
   
    # set thread name to a known value which is useful for debugging purposes
    ost.name="qaStressTest-thread-"+str(x)
    ost.start()
    threads.append(ost)

for thread in threads:
    thread.join()
    thread.test_finished()

#cleanup

OpenStackThread.log_message("Start cleaning up...")

OpenStackThread.log_message("Clean up snapshots...") 
snapshots = cindercl.volume_snapshots.list(True)
for sp in snapshots:
    if OpenStackThread.SNAPSHOT_NAME in sp.display_name:
        try:
            sp = cindercl.volume_snapshots.get(sp.id)
        except:
            continue
    if sp.status == "available" or sp.status == "error":
        _finish_delete_snapshot (sp)
    elif sp.status == "creating":
        OpenStackThread.log_message("Snapshot %s in creating state, wait" % (sp.id)) 
        w_time = mytime.time()
        while  sp.status == "creating" and (mytime.time() - w_time < WAIT_TIME*60 ):
            mytime.sleep(10)
            sp = cindercl.volume_snapshots.get(sp.id)
     
        if sp.status == "available" or sp.status == "error":
            _finish_delete_snapshot (sp)
        else:
            OpenStackThread.log_message("Unable to delete snapshot %s after wait" % (sp.id))
    else:
        OpenStackThread.log_message("Unable to delete snapshot %s, status is %s" % (sp.id, sp.status))
                
OpenStackThread.log_message("Finished cleaning up snapshots") 
    
OpenStackThread.log_message("Clean up volumes...") 
volumes = cindercl.volumes.list(True)
for vol in volumes:
    if OpenStackThread.VOLUME_NAME in vol.display_name:
        if vol.status == "available" or vol.status == "error":
            _finish_delete_volume (vol)
        elif vol.status == "in-use":
            while True:
                try:
                    OpenStackThread.log_message("Trying to detach volume %s from server %s" % (vol.id,vol.attachments[0]['server_id'])) 
                    novacl.volumes.delete_server_volume(vol.attachments[0]['server_id'], vol.id)
                    
                    # wait for detach to occur
                    w_time = mytime.time()
                    while  vol.status == "in-use" and (mytime.time() - w_time < WAIT_TIME*60 ):
                        mytime.sleep(10)
                        vol = cindercl.volumes.get(vol.id)
                    
                    if vol.status == "available" or vol.status == "error":
                        _finish_delete_volume (vol)
                    else:
                        OpenStackThread.log_message("### Unable to detach volume %s from server %s, status is %s" % (vol.id,vol.attachments[0]['server_id'],vol.status))
                        OpenStackThread.log_message("Will try to delete volume %s anyway" % (vol.id))
                        
                        # lets just try to delete the volume anyway
                        _finish_delete_volume (vol) 
                        
                    break
                except novaex.OverLimit:
                    # wait and then retry
                    OpenStackThread.log_message("Detaching volume %s from server %s failed, overlimit; will sleep and retry" % (vol.id,vol.attachments[0]['server_id'])) 
                    mytime.sleep(WAIT_RETRY)
                    continue
                except:
                    OpenStackThread.log_message("### Detach failed: %s" % (traceback.format_exc())) 
                    OpenStackThread.log_message("### Cannot detach volume %s from server %s" % (vol.id,vol.attachments[0]['server_id'])) 
                    break

OpenStackThread.log_message("Finished cleaning up volumes") 

if args.keepvm == False:
    servers = novacl.servers.list(True)
    if len(servers) > 0 : 
        OpenStackThread.log_message("Clean up servers...") 
        for sv in servers:
            if OpenStackThread.SERVERS_NAME in sv.name:
                try:
                    OpenStackThread.log_message("Trying to delete server %s with ID %s" % (sv.name,sv.id))
                    novacl.servers.delete(sv)
                    OpenStackThread.log_message("Deleted server %s" % (sv.name))
                        
                    # wait until the server is really deleted which will cause an exception error
                    ctr = 0
                    while True:
                        server = novacl.servers.get(sv.id)
                        status = server._info.get("status")
                        OpenStackThread.log_message("Server %s status: %s" % (sv.name, status))
                        if status == "DELETED":
                            break
                        mytime.sleep(5)
                        ctr += 1
                        if ctr > 12:
                            # something is wrong
                            OpenStackThread.log_message("### Something is wrong, Server %s status %s is not changing" % (sv.name, status))
                            break
                        
                except novaex.NotFound:
                    OpenStackThread.log_message("NotFound exception, meaning the server %s is deleted" % (sv.name))
                except:
                    OpenStackThread.log_message ("### Exception error in trying to delete server %s:" % (sv.name))
                    OpenStackThread.log_message (traceback.format_exc())
                    continue
        OpenStackThread.log_message("Finished cleaning up servers")

OpenStackThread.log_message("Total run actions: "+str(totalRunActions))
OpenStackThread.log_message("Total run action create: "+str(totalRunAction_create))
OpenStackThread.log_message("Total run action delete: "+str(totalRunAction_delete))
OpenStackThread.log_message("Total run action create snapshot: "+str(totalRunAction_create_sp))
OpenStackThread.log_message("Total run action delete snapshot: "+str(totalRunAction_delete_sp))
OpenStackThread.log_message("Total run action attach: "+str(totalRunAction_attach))
OpenStackThread.log_message("Total run action detach: "+str(totalRunAction_detach))
        
OpenStackThread.log_message("Total run errors: "+str(totalRunErrors))
OpenStackThread.log_message("Total run create volume errors:"+str(totalRunError_create))
OpenStackThread.log_message("Total run delete volume errors:"+str(totalRunError_delete))
OpenStackThread.log_message("Total run create snapshot errors:"+str(totalRunError_create_sp))
OpenStackThread.log_message("Total run delete snapshot errors:"+str(totalRunError_delete_sp))
OpenStackThread.log_message("Total run attach volume errors:"+str(totalRunError_attach))
OpenStackThread.log_message("Total run detach volume errors:"+str(totalRunError_detach))

OpenStackThread.log_message("Attachment distribution:"+str(OpenStackThread.attachCounters))
OpenStackThread.log_message("Done")

