import os
import base64

from email.message import EmailMessage
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dotenv import load_dotenv
from logs.status_logger import logger


load_dotenv()


SCOPES = ['https://www.googleapis.com/auth/gmail.send']

def get_token(creds_path, scopes):
    token = None


    if os.path.exists(r'src\mail_utils\gmail_utils\token_creds.json'):
        try:
            token = Credentials.from_authorized_user_file(r'src\mail_utils\gmail_utils\token_creds.json', scopes)
        except Exception as e:
            logger('error',f"Invalid token_creds.json file: {e}")
            os.remove(r'src\mail_utils\gmail_utils\token_creds.json') 
            token = None


    if not token or not token.valid:
        if token and token.expired and token.refresh_token:
            logger('info',"Refreshing token...")
            token.refresh(Request())
        else:
            logger('info',"Generating new token. Follow the browser instructions.")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, scopes)
            token = flow.run_local_server(port=0)

        with open(r'src\mail_utils\gmail_utils\token_creds.json', 'w') as token_file:
            token_file.write(token.to_json())

    return token


def send_email(service, sender, recipient, subject, body):
    try:
        message = EmailMessage()
        message.set_content(body, subtype='html')
        message['To'] = recipient
        message['From'] = sender
        message['Subject'] = subject

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

        payload = {'raw': encoded_message}

        sent_message = service.users().messages().send(userId="me", body=payload).execute()
        logger('info',f"Email sent successfully! Message ID: {sent_message['id']}")

    except HttpError as error:
        logger('error',f"An error occurred: {error}")


def passer(creds_path,sender_email,recipient_email,subject,body):



    token = get_token(creds_path, SCOPES)
    service = build('gmail', 'v1', credentials=token)

    send_email(service, sender_email, recipient_email, subject, body)

