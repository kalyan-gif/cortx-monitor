"""
 ****************************************************************************
 Filename:          raid.py
 Description:       Monitors /proc/mdstat for changes and notifies
                    the node_data_msg_handler when a change is detected
 Creation Date:     07/16/2015
 Author:            Jake Abernathy

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""
import os
import json
import time
import subprocess
import socket
import uuid

from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger
from framework.utils.severity_reader import SeverityReader

# Modules that receive messages from this module
from message_handlers.node_data_msg_handler import NodeDataMsgHandler
from message_handlers.logging_msg_handler import LoggingMsgHandler

from zope.interface import implementer
from sensors.Iraid import IRAIDsensor

@implementer(IRAIDsensor)
class RAIDsensor(ScheduledModuleThread, InternalMsgQ):


    SENSOR_NAME       = "RAIDsensor"
    PRIORITY          = 1
    RESOURCE_TYPE     = "node:os:raid"

    # Section and keys in configuration file
    RAIDSENSOR        = SENSOR_NAME.upper()
    RAID_STATUS_FILE  = 'RAID_status_file'

    RAID_CONF_FILE    = '/etc/mdadm.conf'
    RAID_DOWN_DRIVE_STATUS = [ { "status" : "_" }, { "status" : "_" } ]

    SYSTEM_INFORMATION = "SYSTEM_INFORMATION"
    SITE_ID = "site_id"
    CLUSTER_ID = "cluster_id"
    NODE_ID = "node_id"
    RACK_ID = "rack_id"

    prev_alert_type = None
    alert_type = None

    # alerts
    FAULT_RESOLVED = "fault_resolved"
    FAULT = "fault"
    MISSING = "missing"
    INSERTION = "insertion"

    @staticmethod
    def name():
        """@return: name of the monitoring module."""
        return RAIDsensor.SENSOR_NAME

    def __init__(self):
        super(RAIDsensor, self).__init__(self.SENSOR_NAME,
                                         self.PRIORITY)
        # Current RAID status information
        self._RAID_status = None

        # Location of hpi data directory populated by dcs-collector
        self._start_delay  = 10

        # Flag to indicate suspension of module
        self._suspended = False

    def initialize(self, conf_reader, msgQlist, product):
        """initialize configuration reader and internal msg queues"""

        # Initialize ScheduledMonitorThread and InternalMsgQ
        super(RAIDsensor, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(RAIDsensor, self).initialize_msgQ(msgQlist)

        self._RAID_status_file = self._get_RAID_status_file()
        logger.info("          Monitoring RAID status file: %s" % self._RAID_status_file)

        # The status file contents
        self._RAID_status_contents = "N/A"

        # The mdX status line in the status file
        self._RAID_status = "N/A"

        self._faulty_drive_list = {}

        self._faulty_device_list = {}

        self._site_id = int(self._conf_reader._get_value_with_default(
                                self.SYSTEM_INFORMATION, self.SITE_ID, 0))
        self._cluster_id = int(self._conf_reader._get_value_with_default(
                                self.SYSTEM_INFORMATION, self.CLUSTER_ID, 0))
        self._rack_id = int(self._conf_reader._get_value_with_default(
                                self.SYSTEM_INFORMATION, self.RACK_ID, 0))
        self._node_id = int(self._conf_reader._get_value_with_default(
                                self.SYSTEM_INFORMATION, self.NODE_ID, 0))

    def read_data(self):
        """Return the Current RAID status information"""
        return self._RAID_status

    def run(self):
        """Run the sensor on its own thread"""

        # Do not proceed if module is suspended
        if self._suspended == True:
            self._scheduler.enter(30, self._priority, self.run, ())
            return

        # Allow systemd to process all the drives so we can map device name to serial numbers
        time.sleep(120)

        # Check for debug mode being activated
        self._read_my_msgQ_noWait()

        # self._set_debug(True)
        # self._set_debug_persist(True)

        try:
            # Check for a change in status file and notify the node data msg handler
            self._notify_NodeDataMsgHandler()
        except Exception as ae:
            logger.exception(ae)

        # Reset debug mode if persistence is not enabled
        self._disable_debug_if_persist_false()

        # Fire every 30 seconds to see if there's a change in RAID status file
        self._scheduler.enter(30, self._priority, self.run, ())

    def _notify_NodeDataMsgHandler(self):
        """See if the status files changed and notify node data message handler
            for generating JSON message"""
        self._drive_state_changed = False
        self._device_state_changed = False
        # resource_id for drive alerts
        resource_id = None
        if not os.path.isfile(self._RAID_status_file):
            logger.warn("status_file: %s does not exist, ignoring." % self._RAID_status_file)
            return

        # Read in status and see if it has changed
        with open(self._RAID_status_file, "r") as datafile:
            status = datafile.read()

        # Do nothing if the RAID status file has not changed
        if self._RAID_status_contents == status:
            self._log_debug("_notify_NodeDataMsgHandler status unchanged, ignoring: %s" % status)
            return

        # Update the RAID status contents of file
        self._RAID_status_contents = status

        # Process mdstat file and send json msg to NodeDataMsgHandler
        md_device_list, drive_list,drive_status_chnaged = self._process_mdstat()

        # checks mdadm conf file for missing raid array and send json message to NodeDataMsgHandler
        self._process_missing_md_devices(md_device_list)

        if md_device_list:
            device = md_device_list[0]
            if device in self._faulty_device_list:
                self.alert_type = self.FAULT_RESOLVED
                self._device_state_changed = True
                del self._faulty_device_list[device]

        if drive_list:
            if len(drive_list) < self._total_drives and \
                self.prev_alert_type != self.MISSING:
                self.alert_type = self.MISSING
                resource_id = self._device+":"
                self._drive_state_changed = True
            if len(drive_list) >= self._total_drives and \
                self.prev_alert_type == self.MISSING:
                self.alert_type = self.INSERTION
                resource_id = self._device+":/dev/"+drive_list[0]
                if drive_list[0] in self._faulty_drive_list:
                    del self._faulty_drive_list[drive_list[0]]
                self._drive_state_changed = True
            if self.alert_type is not None:
                if self._device_state_changed == True:
                    self._resource_id = self._device
                if self._drive_state_changed == True:
                    self._resource_id = resource_id
                self._send_json_msg(self.alert_type,self._resource_id)

            if drive_status_chnaged:
                for drive in self._drives:
                    if drive.get("identity") is not None:
                        drive_path = drive.get("identity").get("path")
                        drive_name = drive_path[5:]
                        resource_id = self._device+":/dev/"+drive_name
                        drive_status = drive.get("status")
                        if drive_status != "U" and drive_name not in self._faulty_drive_list:
                            self.alert_type = self.FAULT
                            self._drive_state_changed = True
                            self._faulty_drive_list[drive_name] = self.alert_type
                        if drive_status == "U" and drive_name in self._faulty_drive_list:
                            self.alert_type = self.FAULT_RESOLVED
                            self._drive_state_changed = True
                            del self._faulty_drive_list[drive_name]
                        if self.alert_type is not None and self._drive_state_changed == True:
                            self._send_json_msg(self.alert_type,resource_id)

    def _process_mdstat(self):
        """Parse out status' and path info for each drive"""
        # Replace new line chars with spaces
        mdstat = self._RAID_status_contents.strip().split("\n")
        md_device_list = []
        drive_list = []
        # list of lines in mdstat which contains "md"
        mdlines = []
        # select 1st raid device to monitor
        monitored_device = []
        drive_status_chnaged = False
        # Array of optional identity json sections for drives in array
        self._identity = {}

        # Read in each line looking for a 'mdXXX' value
        md_line_parsed = False
        for line in mdstat:
            fields = line.split(" ")
            if "md" in fields[0]:
                mdlines.append(line)
                self._device = "/dev/{}".format(fields[0])
                self._log_debug("md device found: %s" % self._device)
                md_device_list.append(self._device)
        if len(md_device_list) > 1:
            logger.warning("Multiple RAID arrays are not supported,%s device not monitored." %self._device)
        if mdlines:
            # find 1st mdarray in mdstat to monitor
            index = mdstat.index(mdlines[-1])
            monitored_device = mdstat[index:]

        for line in monitored_device:
            # The line following the mdXXX : ... contains the [UU] status that we need
            if md_line_parsed is True:
                # Format is [x/y][UUUU____...]
                drive_status_chnaged = self._parse_raid_status(line)
                # Reset in case their are multiple configs in file
                md_line_parsed = False

            # Break the  line apart into separate fields
            fields = line.split(" ")

            # Parse out status' and path info for each drive
            if "md" in fields[0]:
                self._device = "/dev/{}".format(fields[0])
                self._log_debug("md device found: %s" % self._device)

                # Parse out raid drive paths if they're present
                for field in fields:
                    if "[" in field:
                        if field not in drive_list:
                            index = field.find("[")
                            drive_name = field[:index]
                            drive_list.append(drive_name)
                        self._add_drive(field)
                md_line_parsed = True
        return md_device_list,drive_list, drive_status_chnaged

    def _add_drive(self, field):
        """Adds a drive to the list"""
        first_bracket_index = field.find('[')

        # Parse out the drive path
        drive_path = "/dev/{}".format(field[: first_bracket_index])

        # Parse out the drive index into [UU] status which is Device Role field
        detail_command = "/usr/sbin/mdadm --examine {} | grep 'Device Role'".format(drive_path)
        response, error = self._run_command(detail_command)

        if error:
            self._log_debug("_add_drive, Error retrieving drive index into status, example: [U_]: %s" %
                            str(error))
        try:
            drive_index = int(response.split(" ")[-1])
        except Exception as ae:
            self._log_debug("_add_drive, get drive_index error: %s" % str(ae))
            return
        self._log_debug("_add_drive, drive index: %d, path: %s" %
                        (drive_index, drive_path))

        # Create the json msg, serial number will be filled in by NodeDataMsgHandler
        identity_data = {
                        "path" : drive_path,
                        "serialNumber" : "None"
                        }
        self._identity[drive_index] = identity_data

    def _parse_raid_status(self, status_line):
        """Parses the status of each drive denoted by U & _
            for drive being Up or Down in raid
        """
        # Parse out x for total number of drives
        first_bracket_index = status_line.find('[')

        # If no '[' found, return
        if first_bracket_index == -1:
            return False

        self._total_drives = int(status_line[first_bracket_index + 1])
        self._log_debug("_parse_raid_status, total_drives: %d" % self._total_drives)

        # Break the  line apart into separate fields
        fields = status_line.split(" ")

        # The last field is the list of U & _
        status = fields[-1]
        self._log_debug("_parse_raid_status, status: %s, total drives: %d" %
                        (status, self._total_drives))

        # See if the status line has changed, if not there's nothing to do
        if self._RAID_status == status:
            self._log_debug("RAID status has not changed, ignoring: %s" % status)
            return False
        else:
            self._log_debug("RAID status has changed, old: %s, new: %s" % (self._RAID_status, status))
            self._RAID_status = status

        # Array of raid drives in json format based on schema
        self._drives = []

        drive_index = 0
        while drive_index < self._total_drives:
            # Create the json msg and append it to the list
            if self._identity.get(drive_index) is not None:
                path = self._identity.get(drive_index).get("path")
                drive_status_msg = {
                                 "status" : status[drive_index + 1],  # Move past '['
                                 "identity": {
                                    "path": path,
                                    "serialNumber": "None"
                                    }
                                }
            else:
               drive_status_msg = {"status" : status[drive_index + 1]}  # Move past '['

            self._log_debug("_parse_raid_status, drive_index: %d" % drive_index)
            self._log_debug("_parse_raid_status, drive_status_msg: %s" % drive_status_msg)
            self._drives.append(drive_status_msg)

            drive_index = drive_index + 1

        return True

    def _process_missing_md_devices(self, md_device_list):
        """ checks the md raid configuration file, compares all it's
            entries with list of arrays from mdstat file and sends
            missing entry in RabbitMQ channel
        """

        if not os.path.isfile(self.RAID_CONF_FILE):
            logger.warn("_process_missing_md_devices, MDRaid configuration file %s is missing" % self.RAID_CONF_FILE)
            return

        conf_device_list = []
        with open(self.RAID_CONF_FILE, 'r') as raid_conf_file:
            raid_conf_data = raid_conf_file.read().strip().split("\n")
        for line in raid_conf_data:
            try:
                raid_conf_field = line.split(" ")
                if "md" in raid_conf_field[1]:
                    conf_device_list.append(raid_conf_field[1])
            except Exception as ae:
                self._log_debug("_process_missing_md_devices, error retrieving raid entry from %s file: %s" \
                % (self.RAID_CONF_FILE, str(ae)))
                return

        # compare conf file raid array list with mdstat raid array list
        for device in conf_device_list:
            if device not in md_device_list:
                # add that missing raid array entry into the list of raid devices
                self._device = device
                prev_drive_status = self._drives
                self._drives  = self.RAID_DOWN_DRIVE_STATUS
                self.alert_type = self.FAULT
                self._faulty_device_list[device] = self.FAULT
                self._send_json_msg(self.alert_type,self._device)
                self._drives = prev_drive_status

    def _send_json_msg(self,alert_type, resource_id):
        """Transmit data to NodeDataMsgHandler to be processed and sent out"""

        epoch_time = str(int(time.time()))
        severity_reader = SeverityReader()
        severity = severity_reader.map_severity(alert_type)
        self._alert_id = self._get_alert_id(epoch_time)
        host_name = socket.getfqdn()

        info = {
                "site_id": self._site_id,
                "cluster_id": self._cluster_id,
                "rack_id": self._rack_id,
                "node_id": self._node_id,
                "resource_type": self.RESOURCE_TYPE,
                "resource_id": resource_id,
                "event_time": epoch_time
               }
        specific_info = {
            "device": self._device,
            "drives": self._drives
                }

        internal_json_msg = json.dumps(
            {"sensor_request_type" : {
                "node_data": {
                    "status": "update",
                    "sensor_type" : "raid_data",
                    "host_id": host_name,
                    "alert_type": alert_type,
                    "alert_id": self._alert_id,
                    "severity": severity,
                    "info": info,
                    "specific_info": specific_info
                    }
                }
            })
        self.prev_alert_type = alert_type
        self.alert_type = None

        self._log_debug("_send_json_msg, internal_json_msg: %s" %(internal_json_msg))

        # Send the event to node data message handler to generate json message and send out
        self._write_internal_msgQ(NodeDataMsgHandler.name(), internal_json_msg)

    def _log_IEM(self):
        """Sends an IEM to logging msg handler"""
        json_data = json.dumps(
            {"sensor_request_type": {
                "node_data": {
                    "status": "update",
                    "sensor_type": "raid_data",
                    "device": self._device,
                    "drives": self._drives
                    }
                }
            }, sort_keys=True)

        # Send the event to node data message handler to generate json message and send out
        internal_json_msg=json.dumps(
                {'actuator_request_type': {'logging': {'log_level': 'LOG_WARNING', 'log_type': 'IEM', 'log_msg': '{}'.format(json_data)}}})

        # Send the event to logging msg handler to send IEM message to journald
        self._write_internal_msgQ(LoggingMsgHandler.name(), internal_json_msg)

    def _get_alert_id(self, epoch_time):
        """Returns alert id which is a combination of
        epoch_time and salt value
        """
        salt = str(uuid.uuid4().hex)
        alert_id = epoch_time + salt
        return alert_id
    def suspend(self):
        """Suspends the module thread. It should be non-blocking"""
        super(RAIDsensor, self).suspend()
        self._suspended = True

    def resume(self):
        """Resumes the module thread. It should be non-blocking"""
        super(RAIDsensor, self).resume()
        self._suspended = False

    def _run_command(self, command):
        """Run the command and get the response and error returned"""
        self._log_debug("_run_command: %s" % command)
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        response, error = process.communicate()

        if response:
            self._log_debug("_run_command, response: %s" % str(response))
        if error:
            self._log_debug("_run_command: error: %s" % str(error))

        return response.decode().rstrip('\n'), error.decode().rstrip('\n')

    def _get_RAID_status_file(self):
        """Retrieves the file containing the RAID status information"""
        return self._conf_reader._get_value_with_default(self.RAIDSENSOR,
                                                        self.RAID_STATUS_FILE,
                                                        '/proc/mdstat')
    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(RAIDsensor, self).shutdown()
