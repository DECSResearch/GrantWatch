import os
import re
from datetime import datetime

from logs.status_logger import logger

FILE_PATH = 'src/grants_data/grants_json_data'

def get_latest_file_path(file_path=FILE_PATH):
    logger("info", f"Getting latest file path from {file_path}...")
    try:
        files = os.listdir(file_path)
        pattern = re.compile(r"grants_data_(\d{8}_\d{6})\.json")
        matching_files = [f for f in files if re.match(pattern, f)]
        
        logger("info", f"Matching files: {len(matching_files)} found.")

        if matching_files:
            matching_files.sort(key=lambda x: datetime.strptime(x.split('_')[2].split('.')[0], '%Y%m%d'), reverse=True)
            latest_file = matching_files[0]
            
            logger("info", f"Latest file: {latest_file}")
            
            file_path= os.path.join(file_path, latest_file)
            date= latest_file.split('_')[2].split('.')[0]
            
            logger("info", f"Date extracted: {date}")
            
            return file_path, date

        else:
            logger("warning", "No matching files found.")
            return None, None
        
    except FileNotFoundError:
        logger("error", f"Directory not found: {file_path}")
        return None, None
    
    except PermissionError:
        logger("error", f"Permission denied: {file_path}")
        return None, None

    except Exception as e:
        logger("error", f"Error getting latest file path: {e}")
        return None, None
    
