import sys

from grants_data.required_data import onlyTheGoodStuff
from grants_data.download_json import gen_grants


#success=gen_grants()
#if not success:
#    sys.exit(1)

onlyTheGoodStuff()


#gpt_summarizer.summarizer()