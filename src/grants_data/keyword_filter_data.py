from logs.status_logger import logger
from rapidfuzz import fuzz


def filter_grants_by_keywords(grants, grants_key, keywords, threshold):
    try:
        logger("info", f"Filtering grants by {len(keywords)} keywords...")
        filtered_grants = []
        
        lower_keywords = [key.lower() for key in keywords]

        for grant in grants:
            description = grant.get(f"{grants_key}", "").lower()
            
            scores = [fuzz.partial_ratio(kw, description) for kw in lower_keywords]
            aggregated_score = sum(scores) / len(scores) if scores else 0
            
            if aggregated_score >= threshold:
                filtered_grants.append(grant)

        logger("info", f"Filtered grants count: {len(filtered_grants)}")
        return filtered_grants
    except Exception as e:
        logger("error", f"Error filtering grants by keywords: {e}")
        return []