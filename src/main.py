import sys

import download_json
import status_logger
import gpt_summarizer


success=download_json.gen_grants()
if not success:
    status_logger.logger("error","CSV file download failed.")
    sys.exit(1)


gpt_summarizer.summarizer()