from config_utils.load_config import config_loader
from logs.status_logger import logger
from llm_utils.conn import request_llm

import json

def description_summarizer(grants):

    logger("info", "Starting description summarization for all grants...")
    config = config_loader()
    if not config:
        logger("critical", "Configuration loading failed. Exiting description summarizer.")
        return None

    model = config.get("llm_model")
    if not model:
        logger("critical", "LLM model not specified in configuration. Exiting description summarizer.")
        return None
    grants_length = len(grants)
    logger("info", f"Total grants to process: {grants_length}")
    for grant in grants:
        try:
            grant_description = grant.get("FUNDING_DESCRIPTION", "")
            if not grant_description:
                logger("warning", f"No funding description provided for grant '{grant.get('OPPORTUNITY_TITLE', 'No Title')}'. Skipping summarization.")
                continue

            prompt = description_prompt_gen(grant)

            response = request_llm(prompt, model)
            if response and "choices" in response and len(response["choices"]) > 0:
                summary = response["choices"][0]["message"]["content"].strip()
                grants_length -= 1
                logger("info", f"Grants left to process: {grants_length}")
                grant["FUNDING_DESCRIPTION"] = summary
            else:
                logger("error", f"LLM response does not contain a valid summary for grant '{grant.get('OPPORTUNITY_TITLE', 'No Title')}'.")
        except Exception as e:
            logger("error", f"An error occurred during description summarization for grant '{grant.get('OPPORTUNITY_TITLE', 'No Title')}': {e}")
    return grants

def description_prompt_gen(grant):

    grant_json_str = json.dumps(grant, indent=2)
    prompt = (
        "Summarize the following grant information in a concise, informative paragraph.\n\n"
        "Grant Information (JSON):\n"
        f"{grant_json_str}\n\n"
        "The summary should not include - opportunity title, agency name and code, posted date, close date, funding amounts, and contact information. Do not include additional commentary."
        "Just use description and links to the grant.\n\n"
    )
    return prompt
