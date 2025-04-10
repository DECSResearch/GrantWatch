import requests
import datetime

from logs.status_logger import logger

       
HEADERS = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://grants.gov",
            "Referer": "https://grants.gov/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.140 Safari/537.36"
            }
        
PAYLOAD = {
        "keyword": None,
        "cfda": None,
        "agencies": None,
        "sortBy": "openDate|desc",
        "rows": 5000,
        "eligibilities": None,
        "fundingCategories": None,
        "fundingInstruments": None,
        "dateRange": "",
        "oppStatuses": "forecasted|posted"
        }

def gen_grants():
    success = False
    logger("info","Downloading CSV file...")
    try:
        url = "https://micro.grants.gov/rest/opportunities/search_export_Mark2"
        response = requests.get(url, stream=True)
 
        
        response = requests.post(url, headers=HEADERS, json=PAYLOAD)
        
        if response.status_code == 200:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(f"src/grants_data/grants_json_data/grants_data_{timestamp}.json", "wb") as file:                
                file.write(response.content)
            logger("info",f"CSV file downloaded successfully as 'grants_data_{timestamp}.json'.")
            success = True
        
        else:
            logger("error",f"Failed to download CSV file. HTTP Status Code:", response.status_code)
        
    except requests.exceptions.RequestException as e:
        logger("error",f"Error downloading {url}: {e}")
    except Exception as e:
        logger("error",f"Error writing to file - grants_data.json: {e}")

    return success