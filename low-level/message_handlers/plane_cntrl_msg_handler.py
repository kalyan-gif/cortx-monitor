"""
 ****************************************************************************
 Filename:          plane_cntrl_msg_handler.py
 Description:       Message Handler for service request messages
 Creation Date:     11/17/2016
 Author:            Jake Abernathy

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""
import json
import syslog

from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger

# Modules that receive messages from this module
from framework.rabbitmq.plane_cntrl_rmq_egress_processor import PlaneCntrlRMQegressProcessor
from json_msgs.messages.actuators.ack_response import AckResponseMsg


class PlaneCntrlMsgHandler(ScheduledModuleThread, InternalMsgQ):
    """Message Handler for plane controller request messages"""

    MODULE_NAME = "PlaneCntrlMsgHandler"
    PRIORITY    = 2


    @staticmethod
    def name():
        """ @return: name of the module."""
        return PlaneCntrlMsgHandler.MODULE_NAME

    def __init__(self):
        super(PlaneCntrlMsgHandler, self).__init__(self.MODULE_NAME,
                                                  self.PRIORITY)

    def initialize(self, conf_reader, msgQlist, products):
        """initialize configuration reader and internal msg queues"""
        # Initialize ScheduledMonitorThread
        super(PlaneCntrlMsgHandler, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(PlaneCntrlMsgHandler, self).initialize_msgQ(msgQlist)

        self._import_products(products)
        
        self._sedOpDispatch = None

    def _import_products(self, products):
        """Import classes based on which product is being used"""
        if "CS-L" in products or \
           "CS-G" in products:
            from sedutil.sedDispatch import SedOpDispatch
            self._SedOpDispatch = SedOpDispatch
            self._SedOpDispatch.setLogger(logger)

    def run(self):
        """Run the module on its own thread blocking for incoming messages."""
        #self._set_debug(True)
        #self._set_debug_persist(True)

        self._log_debug("Start accepting requests")

        try:
            # Block on message queue until it contains an entry
            jsonMsg = self._read_my_msgQ()
            if jsonMsg is not None:
                self._process_msg(jsonMsg)

            # Keep processing until the message queue is empty
            while not self._is_my_msgQ_empty():
                jsonMsg = self._read_my_msgQ()
                if jsonMsg is not None:
                    self._process_msg(jsonMsg)

        except Exception as ae:
            # Log it and restart the whole process when a failure occurs
            logger.exception("PlaneCntrlMsgHandler restarting: %s" % str(ae))

        self._scheduler.enter(1, self._priority, self.run, ())
        self._log_debug("Finished processing successfully")

    def _process_msg(self, jsonMsg):
        """Parses the incoming message and process"""

        if isinstance(jsonMsg, dict) == False:
            jsonMsg = json.loads(jsonMsg)

        # Parse json msg into usable fields
        success = self._parse_jsonMsg(jsonMsg)
        if not success:
            response = "An error occurred parsing JSON fields"
            self._send_response(response)
            return

        try:
            self._sedOpDispatch = self._SedOpDispatch(self._command, self._parameters, self._arguments)
            status = self._sedOpDispatch.status

            # Don't continue on init errors, invalid command or doesn't apply to this node
            if self._sedOpDispatch.status != 0:
                if self._sedOpDispatch.status == 2:
                    self._log_debug("_process_msg, request is not for this node, ignoring.")
                else:
                    errors = self._sedOpDispatch.errors
                    self._log_debug("_process_msg, status: %s, errors: %s" % \
                                    (str(self._sedOpDispatch.status), str(errors)))
                return

            # Let the egress processor know the current task being worked
            self._write_internal_msgQ(PlaneCntrlRMQegressProcessor.name(), jsonMsg)

            hostname = self._sedOpDispatch.hostname
            response = "N/A"
            errors   = "N/A"

            # Run the command with the parameters and arguments and retrive the response and any errors
            status   = self._sedOpDispatch.run()
            response = self._sedOpDispatch.output
            errors   = self._sedOpDispatch.errors

            self._log_debug("PlaneCntrlMsgHandler, _process_msg, status: %s, command: %s, parameters: %s, args: %s" % \
                        (str(status), str(self._command), str(self._parameters), str(self._arguments)))
            self._log_debug("PlaneCntrlMsgHandler, _process_msg, response: %s" % str(response))
            self._log_debug("PlaneCntrlMsgHandler, _process_msg, errors: %s" % str(errors))
        except Exception as ae:
            errors = str(ae)
            logger.warn("PlaneCntrlMsgHandler, _process_msg exception: %s" % errors)
            response = "There was an error processing the request.  Please refer to the logs for details."

        # No need to enable self._sedOpDispatch.interrupt() in the shutdown()
        self._sedOpDispatch = None

        # Transmit the response back as an Ack json msg
        self._send_response(status, hostname, response, errors)

    def _send_response(self, status, hostname, response, errors):
        """Transmit the response back as an Ack json msg"""
        ack_type = {}
        ack_type["hostname"]    = unicode(hostname, 'utf-8')
        ack_type["command"]     = self._command
        ack_type["parameters"]  = self._parameters
        ack_type["status"]      = status
        ack_type["errors"]      = unicode(errors, 'utf-8')

        ack_msg = AckResponseMsg(json.dumps(ack_type), \
                                 str(response), self._uuid).getJson()
        self._write_internal_msgQ(PlaneCntrlRMQegressProcessor.name(), ack_msg)

    def _parse_jsonMsg(self, jsonMsg):
        """Parse json msg into usable fields"""
        try:
            # Parse out the uuid so that it can be sent back in Ack message
            self._uuid = None
            if jsonMsg.get("sspl_ll_msg_header") is not None and \
               jsonMsg.get("sspl_ll_msg_header").get("uuid") is not None:
                self._uuid = jsonMsg.get("sspl_ll_msg_header").get("uuid")

            # Parse out values from msg
            self._command    = jsonMsg.get("actuator_request_type").get("plane_controller").get("command")
            self._parameters = jsonMsg.get("actuator_request_type").get("plane_controller").get("parameters")
            self._arguments  = jsonMsg.get("actuator_request_type").get("plane_controller").get("arguments")

            # Ignore incorrectly formatted messages
            if self._command is None:
                logger.warn("PlaneCntrlMsgHandler, _parse_jsonMsg, command is none")
                logger.warn("PlaneCntrlMsgHandler, _process_msg, command: %s" % str(self._command))
                return False

            return True
        except Exception as ae:
            logger.warn("PlaneCntrlMsgHandler, _parse_jsonMsg: %s" % str(ae))
            return False

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        logger.info("PlaneCntrlMsgHandler, thread shutting down")

        # Cleanup
        super(PlaneCntrlMsgHandler, self).shutdown()

        # Interrupt any current SED operations
        if self._sedOpDispatch is not None:
            try:
                logger.info("PlaneCntrlMsgHandler, calling sedOpDispatch.interrupt()")
                self._sedOpDispatch.interrupt()
            except Exception as ae:
                logger.warn("PlaneCntrlMsgHandler, shutdown, _sedOpDispatch.interrupt exception: %s" % str(ae))