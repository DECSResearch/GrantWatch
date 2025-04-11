import sys

from grants_data.required_data import onlyTheGoodStuff
from mail_utils.gmail_utils.gmail_sender import parse_send_grants


success , grants= onlyTheGoodStuff()
if not success:
    sys.exit(1)
    
# summarize the good stuff
#to-do: add the summarizer function to summarize the json data


# send the good stuff to the emailer
parse_send_grants(grants)