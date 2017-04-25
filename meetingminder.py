from __future__ import print_function

debug = 1

## This program scans a particular user'ss Google Calendars to find entries
## that are currently happening or about to happen - the idea is to feed
## the results to an IoT device (via MQTT and/or Particle Cloud) placed inside
## each conference room to alert the occupants that the room has an upcoming
## reservation and they should wrap it up and GTH out!

## Green:   Room reserved, safe, nobody can take it
## Blue:    Squatting. Someone can take it! (blue? dim white[z]?)
## Red:     Meeting over soon, someone's waiting!! GTF OUT!
## Orange:  Meeting over soon, but nobody's waiting

## Site-specific Variable setup:

## Make sure you setup particles.json

# MQTT
mqtt_server = "192.168.210.77"

import traceback
import httplib2
import os
import pytz
from pyrfc3339 import parse

from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

try:
    from paho.mqtt.publish import single
except ImportError:
    mqtt = False
else:
    mqtt = True

import time
import datetime

import json
import requests

try:
    from argparse import ArgumentParser

    flags = ArgumentParser(parents=[tools.argparser]).parse_args()
except ImportError:
    flags = None

# If modifying these scopes, delete your previously saved credentials
# at ~/.credentials/calendar-python-quickstart.json
SCOPES = 'https://www.googleapis.com/auth/calendar.readonly'
CLIENT_SECRET_FILE = 'client_secret.json'
APPLICATION_NAME = 'Google Calendar API Python Quickstart'

# Get the list of Particles associated with my account
try:
    with open('particles.json') as df:
        particle = json.load(df)
except:
    print("Please create a JSON file of your Particles")
    print("named particles.json in the working directory")
    quit(99)


def get_credentials():
    ## This is from Google's Python example code:
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    home_dir = os.path.expanduser('~')
    credential_dir = os.path.join(home_dir, '.credentials')
    if not os.path.exists(credential_dir):
        os.makedirs(credential_dir)
    credential_path = os.path.join(credential_dir,
                                   'calendar-python-quickstart.json')

    store = Storage(credential_path)
    credentials = store.get()
    if not credentials or credentials.invalid:
        flow = client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
        flow.user_agent = APPLICATION_NAME
        if flags:
            credentials = tools.run_flow(flow, store, flags)
        else:  # Needed only for compatibility with Python 2.6
            credentials = tools.run(flow, store)
        print('Storing credentials to ' + credential_path)
    return credentials


# This does the actual "send" to the client, via MQTT and via Particle Cloud
def send(where, what, when):
    # when is redundant, since it's in the What string... but whatever, it might be useful

    # push to mqtt if it's configured
    if mqtt:
        try:
            single("gcal/" + where + "/LEDstring", payload=what, hostname=mqtt_server, qos=1)
        except:
            # If you've configured MQTT and are having problems, here's a good place for debug statements
            print("Failed to send to MQTT: ", traceback.format_exc())
            pass

    # Send to Particle cloud, where each room might have multiple Particle InternetButton devices
    # -uses Particle REST API (https://docs.particle.io/reference/api/)
    if where in particle["particles"]:
        for p in particle["particles"][where].split(','):
            try:
                if debug: print("Particle: ", where, p)
                r = requests.post('https://api.particle.io/v1/devices/%s/LEDs' % p,
                                  data={'args': what, 'access_token': particle["accessToken"]})
                if r.status_code != 200: print("Particle returned: ", r.text)
            except:
                print("ERR: Particle Cloud POST failed")

    return


# Send same code to Particle devices, can be used for errors
def sendAllP(msg="bzzzzrzzzzb"):
    for k in particle.keys():
        send(k, msg, 0)
    return


def main():
    """
    Creates a Google Calendar API service object and outputs a list of the next
    10 events on the user's calendar.
    """

    global err
    credentials = get_credentials()
    http = credentials.authorize(httplib2.Http())

    ## Setting up the GCal API fails sometimes... so let's try multiple
    ## times instead of just giving up after one miserable failure:
    success = False
    for t in range(1, 5):
        try:
            service = discovery.build('calendar', 'v3', http=http)
        except:
            err = traceback.format_exc()  # Save the error message to print out
            print("GCal API Connect failed: waiting ", t, " seconds")
            time.sleep(t)  ##Wait t seconds before trying again
        else:
            success = True
            break

    # So... did we succeed in making the GCal API connection?
    if (not success):
        print("ERR: Failed to connect to Google Calendar API: ", err)
        sendAllP()  # Error code to Particles
        quit(99)  #No sense continuing if GCal won't answer

    # We have a connection to GCal, let's query it for Events!
    page_token = None
    while True:
        calendar_list = service.calendarList().list(pageToken=page_token).execute(num_retries=3)
        for calendar_list_entry in calendar_list['items']:

            calName = calendar_list_entry['summary']  # The Calendar's name - this should match setup above
            calId = calendar_list_entry['id']
            timeZone = calendar_list_entry['timeZone']  # Use the Calendar's timezone

            # Let's only scan those we care about, saving API calls:
            if calName not in particle["particles"]:
                continue

            # A bit more setup of variables we'll need below
            now_s = datetime.datetime.now(pytz.timezone(timeZone)).isoformat()

            endOfDay = datetime.datetime(year=datetime.datetime.utcnow().year,
                                         month=datetime.datetime.utcnow().month,
                                         day=datetime.datetime.utcnow().day,
                                         hour=23, minute=59, second=59).isoformat() + 'Z'  # 'Z' indicates UTC time

            eventsResult = service.events().list(
                calendarId=calId, timeMin=now_s, timeMax=endOfDay, maxResults=2,
                singleEvents=True, orderBy='startTime').execute()

            events = eventsResult.get('items', [])

            # If there are events, let's figure out how long until the next one
            ##NOTE: If a meeting spans multiple conference rooms, this system doesn't work right!
            ##       The logic does not handle that situation!
            howLong = 9999
            inEvent = False
            upcomingEvent = False
            nxtEvent = ''

            for event in events:
                start_s = event['start'].get('dateTime', event['start'].get('date'))
                end_s = event['end'].get('dateTime', event['end'].get('date'))
                try:
                    end = parse(end_s)
                    now = parse(now_s)
                    start = parse(start_s)
                    eventName = event['summary']
                except:
                    ##Probably an all-day event - skip those completely
                    continue


                # skip past events, though this shouldn't ever happen
                if now > end:
                    print("  This one's already over... we should never get here")
                    print("")
                    continue

                # Is an official meeting is happening NOW?
                if now > start and now < end:
                    inEvent = True
                    howLong = round((end - now).total_seconds() / 60) + 1

                    if debug:
                        print("Calendar ", calName, " entry ", eventName, " is NOW.")

                    continue

                # Is a Meeting is coming up very soon?
                if now < start:
                    hl = int((start - now).total_seconds() / 60) + 1

                    if debug:
                        print("Calendar ", calName, " entry ", eventName, " is coming up in ", hl, " minutes")

                    if hl<10:
                        howLong = hl
                        upcomingEvent = True

                    continue

                    # End of for event in events loop in case you're not familiar with Python indenting!

            # Now that we've gone through all this calendar's events (if any), we should know howLong
            # until the next one, so now we can now report on it
            if inEvent:
                str = "g" * 11 + "y"  ##Green means reserved, safe, nobody can take it
            else:
                str = "B" * 11 + "Y"  ##This means you're squatting. Someone can take it! (blue? dim white[z]?)

            if upcomingEvent:
                str = str[-howLong:] + "R" * 11  ##Meeting over soon, someone's waiting!! GTH OUT!
            else:
                str = str[-howLong:] + "o" * 11  ##Meeting over soon, but nobody's waiting

            str = str[:11]  # We only want to send 11 minutes of data to the 11-LED InternetButton device

            print("Sending: ", str, " to ", calName)
            send(calName, str, howLong)

        page_token = calendar_list.get('nextPageToken')
        if not page_token:
            break


if __name__ == '__main__':
    main()
