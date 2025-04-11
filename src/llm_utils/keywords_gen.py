from config_utils.load_config import config_loader
from logs.status_logger import logger
from llm_utils.conn import request_llm

def keyword_extractor():
    try:
        logger("info", "Starting keyword extraction...")
        config = config_loader()
        if not config:
            logger("critical", "Configuration loading failed. Exiting keyword extractor.")
            return None , None

        model = config.get("llm_model")
        if not model:
            logger("critical", "Model not specified in configuration. Exiting keyword extractor.")
            return None , None

        keywords = config.get("keywords")
        if not keywords:
            logger("error", "Keywords not specified in configuration. Exiting keyword extractor.")
            return None , None
        
        number_of_keywords = config.get("number_of_keywords")
        if not number_of_keywords:
            logger("warning", "Number of keywords not specified in configuration. Using default value of 30.")
            return None , None
        
        threshold = config.get("threshold")
        if not threshold:
            logger("warning", "Threshold not specified in configuration. Using default value of 80.")
            return None , 80 
        
        content= keyword_prompt_gen(keywords, number_of_keywords)

        response = request_llm(content, model)
        if response and "choices" in response and len(response["choices"]) > 0:
            keywords = response["choices"][0]["message"]["content"]
            keywords = keywords.split(",")
            keywords = [keyword.strip() for keyword in keywords if keyword.strip()]
            logger("info", f"Keywords are extracted")
            return keywords , threshold
        else:
            logger("error", "Failed to extract keywords from LLM response.")
            return None , None
    except Exception as e:
        logger("error", f"An error occurred during keyword extraction: {e}")
        return None , None
    
def keyword_prompt_gen(keywords, number_of_keywords):
    logger("info", "Generating keyword prompt...")
    prompt = f"Generate more keywords for the following for grants opportunity filtering (include the input keywords also, or exceed {number_of_keywords} words):\n\n"
    prompt += f"{keywords}\n\n"
    prompt += "Please provide a list of keywords that are derived to the above content. Please do not include any other text or explanation. Just provide the keywords in a comma-separated format.\n\n"
    return prompt