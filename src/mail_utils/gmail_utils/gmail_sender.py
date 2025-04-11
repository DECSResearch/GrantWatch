from mail_utils.gmail_utils.gmail import passer
from logs.status_logger import logger
from mail_utils.grants_html_gen import html_maker

def parse_send_grants(grants):
    try:
        logger('info',"Preparing to send mail")
        creds_path = r'src\mail_utils\gmail_utils\api_creds.json' 
        sender_email = "c2sr.und@gmail.com"

        recipient_email = get_emails()
        if recipient_email is None: return None

        subject = "Grants Data Update"
        if recipient_email is None:
            logger('error',"Recipient email not found")
            return

        body = html_maker(grants)

        passer(creds_path,sender_email,recipient_email, subject, body)
    except Exception as e:
        logger('error',f"Unable to send email: {e}")
        return
    
def get_emails():
    file_path="src/mail_utils/recipient_mails.txt"
    try:
        with open(file_path) as f:
            raw_mails = f.read()
        emails=raw_mails.split(";")
        if len(emails) == 0:
            logger('error',"No emails found in the file")
            return None
        return emails
    except Exception as e:
        logger('error',f"Unable to email file: {e}")
        return None


