def html_maker(grants):
    
    grant_items = ""
    for grant in grants:
        title = grant.get("OPPORTUNITY_TITLE", "No Title Provided")
        number = grant.get("OPPORTUNITY_NUMBER", "N/A")
        link = grant.get("OPPORTUNITY_NUMBER_LINK", "#")
        agency = grant.get("AGENCY_NAME", "N/A")
        agency_code = grant.get("AGENCY_CODE", "N/A")
        posted_date = grant.get("POSTED_DATE", "N/A")
        close_date = grant.get("CLOSE_DATE", "N/A")
        description = grant.get("FUNDING_DESCRIPTION", "No description available")
        estimated_total_funding = grant.get("ESTIMATED_TOTAL_FUNDING", "N/A")
        expected_number_of_awards = grant.get("EXPECTED_NUMBER_OF_AWARDS", "N/A")
        award_ceiling = grant.get("AWARD_CEILING", "N/A")
        award_floor = grant.get("AWARD_FLOOR", "N/A")
        grantor_contact = grant.get("GRANTOR_CONTACT", "N/A")

        grant_items += f"""
        <div class="grant">
            <h2><a href="{link}" target="_blank">{title}</a></h2>
            <p class="details">
                <strong>Opportunity Number:</strong> {number}<br>
                <strong>Agency:</strong> {agency} ({agency_code})<br>
                <strong>Posted Date:</strong> {posted_date}<br>
                <strong>Close Date:</strong> {close_date}<br>
                <strong>Estimated Total Funding:</strong> {estimated_total_funding}<br>
                <strong>Expected Number of Awards:</strong> {expected_number_of_awards}<br>
                <strong>Award Ceiling:</strong> {award_ceiling}<br>
                <strong>Award Floor:</strong> {award_floor}<br>
                <strong>Grantor Contact:</strong> {grantor_contact}
            </p>
            <div class="description">
                {description}
            </div>
        </div>
        """

    html_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Grant Opportunities</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background-color: #f2f2f2;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            background-color: #fff;
            padding: 20px;
            border-radius: 5px;
            max-width: 800px;
            margin: auto;
        }}
        .grant {{
            border-bottom: 1px solid #ccc;
            margin-bottom: 20px;
            padding-bottom: 20px;
        }}
        .grant:last-child {{
            border-bottom: none;
            margin-bottom: 0;
            padding-bottom: 0;
        }}
        h2 {{
            margin: 0 0 10px 0;
        }}
        .details {{
            font-size: 0.9em;
            color: #555;
            line-height: 1.5;
        }}
        .description {{
            margin-top: 10px;
        }}
        a {{
            color: #1a0dab;
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Grant Opportunities</h1>
        <p>Please find below the latest grant opportunities:</p>
        {grant_items}
    </div>
</body>
</html>"""

    return html_body