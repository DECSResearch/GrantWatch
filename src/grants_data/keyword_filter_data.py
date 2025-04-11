from logs.status_logger import logger
from rapidfuzz import fuzz


def filter_grants_by_keywords(grants, keywords, threshold):
    try:
        logger("info", f"Filtering grants by {len(keywords)} keywords...")
        filtered_grants = []
        
        lower_keywords = [key.lower() for key in keywords]

        for grant in grants:
            description = grant.get("FUNDING_DESCRIPTION", "").lower()
            
            for kw in lower_keywords:
                score = fuzz.partial_ratio(kw, description)
                if score >= threshold:
                    filtered_grants.append(grant)
                    break

        logger("info", f"Filtered grants count: {len(filtered_grants)}")
        return filtered_grants
    except Exception as e:
        logger("error", f"Error filtering grants by keywords: {e}")
        return []