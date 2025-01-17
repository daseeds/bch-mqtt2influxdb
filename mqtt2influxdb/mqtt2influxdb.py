#!/usr/bin/env python3

import os
import sys
import logging
import json
from datetime import datetime
import paho.mqtt.client
from paho.mqtt.client import topic_matches_sub
import influxdb
import jsonpath_ng
import requests
import base64
from requests.auth import HTTPBasicAuth
import http.client as http_client
import builtins
import py_expression_eval
import pycron
from .expr import variable_to_jsonpath
from .config import json_path
import influxdb_client
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from .sparkplug_b import *




class Mqtt2InfluxDB:

    def __init__(self, config):

        self._points = None
        if 'points' in config:
            self._points = config['points']
        self._pointsSpb = None
        if 'pointsSpb' in config:
            self._pointsSpb = config['pointsSpb']
        self._config = config

        self._influxdb = influxdb.InfluxDBClient(config['influxdb']['host'],
                                                 config['influxdb']['port'],
                                                 config['influxdb'].get('username', 'root'),
                                                 config['influxdb'].get('password', 'root'),
                                                 ssl=config['influxdb'].get('ssl', False))

        self._influxdb2 = InfluxDBClient(url=config['influxdb']['url'], token=config['influxdb']['token'], org="Libre")
        self._write_api = self._influxdb2.write_api(write_options=SYNCHRONOUS,batch_size=8, flush_interval=8)

        self._bucket = config['influxdb']['bucket']

        self._mqtt = paho.mqtt.client.Client()

        if config['mqtt'].get('username', None):
            self._mqtt.username_pw_set(config['mqtt']['username'],
                                       config['mqtt'].get('password', None))

        if config['mqtt'].get('cafile', None):
            self._mqtt.tls_set(config['mqtt']['cafile'],
                               config['mqtt'].get('certfile', None),
                               config['mqtt'].get('keyfile', None))

        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message = self._on_mqtt_message

    def run(self):
        # logging.debug('InfluxDB create database %s', self._config['influxdb']['database'])
        # self._influxdb.create_database(self._config['influxdb']['database'])
        # self._influxdb.switch_database(self._config['influxdb']['database'])

        # for point in self._points:
        #     if 'database' in point:
        #         logging.debug('InfluxDB create database %s', point['database'])
        #         self._influxdb.create_database(point['database'])

        logging.info('MQTT broker host: %s, port: %d, use tls: %s',
                     self._config['mqtt']['host'],
                     self._config['mqtt']['port'],
                     bool(self._config['mqtt'].get('cafile', None)))

        self._mqtt.connect_async(self._config['mqtt']['host'], self._config['mqtt']['port'], keepalive=10)
        self._mqtt.loop_forever()

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        logging.info('Connected to MQTT broker with code %s', rc)

        lut = {paho.mqtt.client.CONNACK_REFUSED_PROTOCOL_VERSION: 'incorrect protocol version',
               paho.mqtt.client.CONNACK_REFUSED_IDENTIFIER_REJECTED: 'invalid client identifier',
               paho.mqtt.client.CONNACK_REFUSED_SERVER_UNAVAILABLE: 'server unavailable',
               paho.mqtt.client.CONNACK_REFUSED_BAD_USERNAME_PASSWORD: 'bad username or password',
               paho.mqtt.client.CONNACK_REFUSED_NOT_AUTHORIZED: 'not authorised'}

        if rc != paho.mqtt.client.CONNACK_ACCEPTED:
            logging.error('Connection refused from reason: %s', lut.get(rc, 'unknown code'))

        if rc == paho.mqtt.client.CONNACK_ACCEPTED:
            if self._points:
                for point in self._points:
                    logging.info('subscribe %s', point['topic'])
                    client.subscribe(point['topic'])
            if self._pointsSpb:
                for pointSpb in self._pointsSpb:
                    topicNode = "spBv1.0/" + pointSpb["groupId"] + "/+/" + pointSpb["nodeName"] + "/#"
                    topicDevice = "spBv1.0/" + pointSpb["groupId"] + "/+/" + pointSpb["nodeName"] + "/#"
                    logging.info('subscribe Spb %s', topicNode)
                    client.subscribe(topicNode)
                    logging.info('subscribe Spb %s', topicDevice)
                    client.subscribe(topicDevice)

    def _on_mqtt_disconnect(self, client, userdata, rc):
        logging.info('Disconnect from MQTT broker with code %s', rc)

    def _on_mqtt_message(self, client, userdata, message):
        logging.debug('mqtt_on_message %s %s', message.topic, message.payload)

        tokens = message.topic.split("/")

        if tokens[0] == "spBv1.0"  :
            for pointSpb in self._pointsSpb:
                if pointSpb["groupId"] == tokens[1] and pointSpb["nodeName"] == tokens[3]:
                    self.manageSpb(message, pointSpb)
        else:
            for point in self._points:
                self.manageStdMqtt(message, point)

    # path: /area/3_STRUCTURE/Equipments/eqpt_id/1_INFORMATIONS/2_VALUE_CHAIN/4_OPERATION_RESPONSE/1_ACTUAL_OUTPUT/1_GOODS
    # path: /area/3_STRUCTURE/Equipments/eqpt_id/4_BEHAVIORS/2_STATES

    # target:   /area/equipments
    #           /area/it
    #           /area/organization
    #           /area/equipment/eqpt_id/2_STRUCTURE/2_DIGITAL/Platform/...
    #           /area/equipment/eqpt_id/2_STRUCTURE/2_DIGITAL/Platform/...
    #           /area/equipment/eqpt_id/2_STRUCTURE/1_PHYSICAL/PC_1/...

    def manageSpb(self, message, point):
        inboundPayload = sparkplug_b_pb2.Payload()
        inboundPayload.ParseFromString(message.payload)

        
        
        for metric in inboundPayload.metrics:
            tokens = metric.name.split("/")
            payload_json = 0
            payload = 0

            # Detect JSON payload
            if metric.datatype == 12 and len(metric.string_value) > 0 and metric.string_value[0] == '{':
                payload_json = 1

                    
            
            if payload_json == 1:
                try:
                    payload = json.loads(metric.string_value)
                except Exception as e:
                    logging.error('parse json: %s topic: %s payload: %s', e, metric.name, metric.string_value)
                    continue
                p = Point(payload["name"])

                for k, v in payload["fields"].items():
                    p.field(k, v)
                for k, v in payload["tags"].items():
                    p.tag(k, v)
            else:
                try:
                    p = Point(tokens[-1])
                except Exception as e:
                    logging.error('exception: %s topic: %s payload: %s', e, metric.name, metric.string_value)

                p.time(metric.timestamp, WritePrecision.MS)
                p.field("value", getMetricValue(metric))
                if len(tokens) < 2:
                    return
                for i in range(2, len(tokens) - 1):
                    p.tag("lvl" + str(i), tokens[i])     

            if (tokens[0] == "Equipments"):
                p.tag("equipment", tokens[1])

            p.tag("site", point["groupId"])
            p.tag("area", point["nodeName"])
       

            line_protocol = p.to_line_protocol()
            print(line_protocol)
            try:
                self._write_api.write(bucket=self._bucket, record=p)
            except Exception as e:
                logging.error("Write Influx failed %s", e)


    def manageStdMqtt(self, message, point):

        msg = None

        for point in self._points:

            if topic_matches_sub(point['topic'], message.topic):
                if not msg:
                    payload = message.payload.decode('utf-8')

                    if payload == '':
                        payload = 'null'
                    try:
                        payload = json.loads(payload)
                    except Exception as e:
                        logging.error('parse json: %s topic: %s payload: %s', e, message.topic, message.payload)
                        return

                    if point['type'] == 'float':
                        payload = float(payload)
                    elif point['type'] == 'int':
                        payload = int(payload)
                    elif point['type'] == 'boolean':
                        payload = (payload == 'True')

                    msg = {
                        "topic": message.topic.split('/'),
                        "payload": payload,
                        "timestamp": message.timestamp,
                        "qos": message.qos
                    }
                if 'schedule' in point:
                    # check if current time is valid in schedule
                    if not pycron.is_now(point['schedule']):
                        logging.info('Skipping %s due to schedule %s' % (message.topic, point['schedule']))
                        continue

                # if payload == '':
                #     payload = 'null'
                # try:
                #     payload = json.loads(payload)
                # except Exception as e:
                #     logging.error('parse json: %s topic: %s payload: %s', e, message.topic, message.payload)
                #     return

                # record = {'measurement': measurement,
                #           'time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                #           'tags': {},
                #           'fields': {}}
                
                

            measurement = self._get_value_from_str_or_JSONPath(point['measurement'], msg)
            if measurement is None:
                logging.warning('unknown measurement')
                return

            p = Point(measurement)

            record = {'measurement': measurement,
                        'time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'tags': {},
                        'fields': {}}

            if 'base64decode' in self._config:
                data = self._get_value_from_str_or_JSONPath(self._config['base64decode']["source"], msg)
                dataDecoded = base64.b64decode(data)
                msg.update({"base64decoded": {self._config['base64decode']["target"]: {"raw": dataDecoded}}})
                dataDecoded = dataDecoded.hex()
                msg.update({"base64decoded": {self._config['base64decode']["target"]: {"hex": dataDecoded}}})

            if 'fields' in point:
                if isinstance(point['fields'], jsonpath_ng.JSONPath):
                    record['fields'] = self._get_value_from_str_or_JSONPath(point['fields'], msg)

                else:
                    for key in point['fields']:
                        if isinstance(point['fields'][key], dict):
                            val = self._get_value_from_str_or_JSONPath(point['fields'][key]['value'], msg)
                            convFunc = getattr(builtins, point['fields'][key]['type'], None)
                            if val is None:
                                continue
                            if convFunc:
                                try:
                                    val = convFunc(val)
                                except ValueError:
                                    val = None
                                    logging.warning('invalid conversion function key')
                        else:
                            val = self._get_value_from_str_or_JSONPath(point['fields'][key], msg)
                            if key == 'value':
                                if isinstance(val, bool):
                                    if 'type' in point['fields'] and point['fields']['type'] == 'booltoint':
                                        val = int(val)
                            elif key == 'type':
                                if val == 'booltoint':
                                    val = 'int'
                        if val is None:
                            logging.warning('Unable to get value for %s' % point['fields'][key])
                            continue
                        record['fields'][key] = val
                        p.field(key, val)
                    if len(record['fields']) != len(point['fields']):
                        logging.warning('different number of fields')

            if not record['fields']:
                logging.warning('empty fields')
                return

            if 'tags' in point:
                for key in point['tags']:
                    val = self._get_value_from_str_or_JSONPath(point['tags'][key], msg)
                    if val is None:
                        logging.warning('Unable to get value for tag %s' % point['tags'][key])
                        continue
                    record['tags'][key] = val
                    p.tag(key, val)

                if len(record['tags']) != len(point['tags']):
                    logging.warning('different number of tags')

            logging.debug('influxdb write %s', record)

            line_protocol = p.to_line_protocol()
            print(line_protocol)

            try:
                self._write_api.write(bucket=self._bucket, record=p)
            except Exception as e:
                print(e)

            #self._influxdb.write_points([record], database=point.get('database', None))

            if 'http' in self._config:
                http_record = {}
                for key in point['httpcontent']:
                    val = self._get_value_from_str_or_JSONPath(point['httpcontent'][key], msg)
                    if val is None:
                        continue
                    http_record.update({key: val})

                action = getattr(requests, self._config['http']['action'], None)
                if action:
                    r = action(url=self._config['http']['destination'], data=http_record, auth=HTTPBasicAuth(self._config['http']['username'], self._config['http']['password']))
                else:
                    logging.error("Invalid HTTP method key!")


    def _get_value_from_str_or_JSONPath(self, param, msg):
        if isinstance(param, str):
            return param

        elif isinstance(param, jsonpath_ng.JSONPath):
            tmp = param.find(msg)
            if tmp:
                return tmp[0].value

        elif isinstance(param, py_expression_eval.Expression):
            vars = {}
            for var in param.variables():
                # must start with JSON__
                if var.startswith('JSON__'):
                    json_field = variable_to_jsonpath(var)
                    tmp = json_path(json_field).find(msg)
                    if tmp:
                        vars[var] = tmp[0].value
                    else:
                        logging.error('unable to find JSON field %s!' % json_field)
                else:
                    logging.error('unknown variable %s in parser expression %s!' % (var, param.toString()))
            logging.debug('evaluating expression %s using the variables %s' % (param.toString(), str(vars)))
            return param.evaluate(vars)
