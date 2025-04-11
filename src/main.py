import sys

from grants_data.required_data import onlyTheGoodStuff


success= onlyTheGoodStuff()
if not success:
    sys.exit(1)
    



#gpt_summarizer.summarizer()