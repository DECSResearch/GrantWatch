import sys

import grants_data.download_json as download_json
import gpt_summarizer
import grants_data.process_json_data as process_json_data

success=download_json.gen_grants()
if not success:
    sys.exit(1)




gpt_summarizer.summarizer()