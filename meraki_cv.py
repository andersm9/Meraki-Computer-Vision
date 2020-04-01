 #!/usr/bin/env python
"""This script:
    launches an MQTT Client
    Subscribes to MV camera flows
    Requests camera snapshot URL from the Meraki API
    Send the snapshot URL to AWS Rekognition
    Receives the response from AWS Recognition
    Sends a formatted response to NodeRed via mqtt """


import configparser
import sys
import json
import requests
import boto3
import paho.mqtt.client as mqtt


def get_meraki_snapshots(api_key, net_id, time=None):
    """Get devices of network"""
    headers = {
        'X-Cisco-Meraki-API-Key': api_key,
        # 'Content-Type': 'application/json'
        # issue where this is only needed if timestamp specified
    }
    response = session.get(f'https://api.meraki.com/api/v0/networks/{net_id}/devices',
                           headers=headers)
    devices = response.json()
    #filter for MV cameras:
    cameras = [device for device in devices if device['model'][:2] == 'MV']
    # Assemble return data
    for camera in cameras:
        #filter for serial number provided
        if camera["serial"] == MV_SERIAL:
            # Get snapshot link
            if time:
                headers['Content-Type'] = 'application/json'
                response = session.post(
                    f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/snapshot',
                    headers=headers,
                    data=json.dumps({'timestamp': time}))
            else:
                response = session.post(
                    f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/snapshot',
                    headers=headers)

            # Possibly no snapshot if camera offline, photo not retrievable, etc.
            if response.ok:
                snapshot_url = response.json()['url']

    return snapshot_url

def gather_credentials():
    """Gather Meraki credentials"""
    conf_par = configparser.ConfigParser()
    try:
        conf_par.read('credentials.ini')
        cam_key = conf_par.get('meraki', 'key')
        net_id = conf_par.get('meraki', 'network')
        mv_serial = conf_par.get('sense', 'serial')
        server_ip = conf_par.get('server', 'ip')
    except:
        print('Missing credentials or input file!')
        sys.exit(2)
    return cam_key, net_id, mv_serial, server_ip

def send_snap_to_aws(image):
    """send the snapshot URL to AWS Rekognition"""
    print("sending snapshot_url to AWS rekognition")
    boto_session = boto3.Session(profile_name='default')
    rek = boto_session.client('rekognition')
    resp = requests.get(image)
    #print(resp)
    rekresp = {}
    resp_txt = str(resp)
    imgbytes = resp.content
    try:
        rekresp = rek.detect_faces(Image={'Bytes': imgbytes}, Attributes=['ALL'])
    except:
        pass

    return(rekresp, resp_txt)

def detect_labels(image, max_labels=10, min_confidence=90):
    """get labels (e.g House, car etc)"""
    rekognition = boto3.client("rekognition")
    resp = requests.get(image)
    imgbytes = resp.content
    label_response = rekognition.detect_labels(
        Image={'Bytes': imgbytes},
        MaxLabels=max_labels,
        MinConfidence=min_confidence,
    )
    #print(str(label_response))
    return label_response['Labels']

def detect_moderation(image, max_labels=10, min_confidence=90):
    """https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/rekognition.html"""
    #rekognition = boto3.client("rekognition")
    resp = requests.get(image)
    imgbytes = resp.content
    moderation_response = client.detect_moderation_labels(
        Image={'Bytes': imgbytes},
        MaxLabels=max_labels,
        MinConfidence=min_confidence,
    )
    print(moderation_response)
    return moderation_response

#get text
def detect_text_detections(image):
    rekognition = boto3.client("rekognition")
    resp = requests.get(image)
    imgbytes = resp.content
    text_response = rekognition.detect_text(
        Image={'Bytes': imgbytes},
    )
    return text_response['TextDetections']

def on_connect(mq_client, userdata, flags, result_code):
    """The callback for when the client receives a CONNACK response from the server"""
    print(f'Connected with result code {result_code}')
    serial = userdata['MV_SERIAL']
    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    mq_client.subscribe(f'/merakimv/{serial}/0')
    #client.subscribe(f'/merakimv/{serial}/light')


def on_message(client, userdata, msg):
    """When a PUBLISH message is received from the server, get a
    URL to analyse"""
    #triggers image analysis when incoming MQTT data is detected
    analyze()

def analyze():
    """periodially takes snap URL from Meraki, sends to AWS rekognition"""
    flag = True
    if flag:
        print("Request Snapshot URL")
        #get the URL of a snapshot from our camera
        snapshot_url = get_meraki_snapshots(API_KEY, NET_ID, None)
        #assume the snapshot is not yet available for download:
        resp_txt = "404"
        while "404" in resp_txt:
            #continually attempt to access snapshot URL
            rekresp, resp_txt = send_snap_to_aws(snapshot_url)

        #once the URL is available (resp_txt != 404), send to
        #AWS Rekognition and print results to stdout
        for face_detail in rekresp['FaceDetails']:
            #print Facial Analysis Results to stdout
            print('Facial Analysis: The detected face is between ' +
                  str(face_detail['AgeRange']['Low']) +
                  'and ' + str(face_detail['AgeRange']['High']) + ' years old')
            age = (((face_detail['AgeRange']['Low'])+(face_detail['AgeRange']['High']))/2)
            #Print Emotion/Gender/Age to stdout
            emotional_state = max(face_detail['Emotions'], key=lambda x: x['Confidence'])
            emotion = emotional_state['Type']
            gender = (face_detail['Gender']['Value'])
            print(gender)
            print(emotion)
            print(age)
            #Publish Emotion/Gender/Age via MQTT to NodeRed

            client.publish("Age", age)
            client.publish("EmotionalState", emotion)
            client.publish("Gender", gender)

        #Publish Object Detection via MQTT to NodeRed
        labels = []
        obj = 0
        objects_detected = detect_labels(snapshot_url)
        quantity_objects = len(objects_detected)
        print("objects_detected recieved")
        #Print to stdout all label names and confidence
        for label in objects_detected:
            #round "Confidence" to three decimal places
            truncated_confidence = str('%.3f' % round((label["Confidence"]), 3))
            detected_object = str("{Name}".format(**label))
            entry = detected_object + " - " + truncated_confidence +" %"
            labels.append(entry)
            label = ("Label" + str(obj))
            #print result to stdout
            print(label + " " + entry)
            #publish to MQTT
            client.publish(label, entry)
            obj = obj+1
        client.publish("Snap", snapshot_url)
        while quantity_objects < 6:
            entry = " - "
            label = ("Label" + str(quantity_objects))
            quantity_objects = quantity_objects + 1
            client.publish(label, entry)
        print("end of objects detected")

        #Print Text Detection via MQTT to NodeRed
        text_detections = []
        text_count= 0
        text_detected = detect_text_detections(snapshot_url)
        quantity_text_detections  = len(text_detections)
        print("text_detections recieved")
        for DetectedText in text_detected:
            truncated_confidence = str('%.3f' % round((DetectedText["Confidence"]),3))
            object = str("{DetectedText}".format(**DetectedText))
            text_entry = object + " - " + truncated_confidence +" %"
            #text_entry = str("{DetectedText} - {Confidence}%".format(**DetectedText))
            text_detections.append(text_entry)
            DetectedText = ("DetectedText" + str(text_count))
            print(DetectedText + " " + text_entry)
            client.publish(DetectedText,text_entry)
            text_count = text_count + 1
        client.publish("Snap",snapshot_url)
        while quantity_text_detections  < 6:
            text_entry = " - "
            DetectedText = ("DetectedText" + str(quantity_text_detections ))
            quantity_text_detections  = quantity_text_detections  + 1
            client.publish(TextDetection, text_entry)
        print("end of text detected")
if __name__ == '__main__':

    (API_KEY, NET_ID, MV_SERIAL, SERVER_IP) = gather_credentials()
    USER_DATA = {
        'API_KEY': API_KEY,
        'NET_ID': NET_ID,
        'MV_SERIAL': MV_SERIAL,
        'SERVER_IP': SERVER_IP
    }
    session = requests.Session()
    # Start MQTT client
    client = mqtt.Client()
    client.user_data_set(USER_DATA)
    #on connection to a MQTT broker:
    client.on_connect = on_connect
    #when an MQTT message is received:
    client.on_message = on_message
    #specify the MQTT broker here
    client.connect(SERVER_IP, 1883, 300)
    client.loop_forever()
