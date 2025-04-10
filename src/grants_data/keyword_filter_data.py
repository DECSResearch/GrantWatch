from logs.status_logger import logger


def filter_grants_by_keywords(grants, keywords):
    try:
        logger("info", f"Filtering grants by {len(keywords)} keywords...")
        filtered_grants = []
        
        lower_keywords = [key.lower() for key in keywords]

        for grant in grants:
            description = grant.get("FUNDING_DESCRIPTION", "")
            description = description.lower()
            if any(kw in description for kw in lower_keywords):
                filtered_grants.append(grant)

        logger("info", f"Filtered grants count: {len(filtered_grants)}")
        return filtered_grants
    except Exception as e:
        logger("error", f"Error filtering grants by keywords: {e}")
        return []