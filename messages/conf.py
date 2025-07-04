
# messages/conf.py
import os
import logging
from typing import List, Optional
from postmarker.core import PostmarkClient

logger = logging.getLogger(__name__)

# Read from environment variables
SKIP_EMAILS = os.getenv('SKIP_EMAILS', 'True').lower() == 'true'
POSTMARK_SERVER_TOKEN = os.getenv('POSTMARK_SERVER_TOKEN', 'POSTMARK_API_TEST')
FROM_EMAIL = os.getenv('FROM_EMAIL', 'noreply@stablevoting.org')
FROM_NAME = os.getenv('FROM_NAME', 'Stable Voting')

# Admin emails
ALL_EMAILS = ['stablevoting.org@gmail.com', 'epacuit@umd.edu', 'wesholliday@berkeley.edu']
SV_EMAIL = ['stablevoting.org@gmail.com']

# Legacy compatibility
email_conf = None  # No longer needed with Postmark


def get_email_client():
    """Get email client instance"""
    if SKIP_EMAILS:
        return None
    return PostmarkClient(server_token=POSTMARK_SERVER_TOKEN)


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    tag: Optional[str] = None
):
    """Send an email using Postmark"""
    if SKIP_EMAILS:
        logger.info(f"[EMAIL SKIPPED] To: {to_email}, Subject: {subject}")
        print(f"[EMAIL SKIPPED] To: {to_email}, Subject: {subject}")
        return {"MessageID": "skipped", "To": to_email}
    
    client = get_email_client()
    if not client:
        raise ValueError("Email client not configured")
    
    # Create text body if not provided
    if not text_body:
        import re
        text_body = re.sub('<[^<]+?>', '', html_body)
    
    try:
        response = client.emails.send(
            From=f"{FROM_NAME} <{FROM_EMAIL}>",
            To=to_email,
            Subject=subject,
            HtmlBody=html_body,
            TextBody=text_body,
            Tag=tag,
            TrackOpens=True,
            TrackLinks="HtmlOnly"
        )
        
        logger.info(f"Email sent: {response['MessageID']} to {to_email}")
        return response
        
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {str(e)}")
        raise


async def send_batch_emails(
    recipients: List[str],
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    tag: Optional[str] = None
):
    """Send batch emails using Postmark"""
    if SKIP_EMAILS:
        logger.info(f"[BATCH EMAIL SKIPPED] {len(recipients)} recipients, Subject: {subject}")
        print(f"[BATCH EMAIL SKIPPED] {len(recipients)} recipients, Subject: {subject}")
        return {"Messages": [{"MessageID": "skipped"} for _ in recipients]}
    
    client = get_email_client()
    if not client:
        raise ValueError("Email client not configured")
    
    # Create text body if not provided
    if not text_body:
        import re
        text_body = re.sub('<[^<]+?>', '', html_body)
    
    # Postmark allows up to 500 messages per batch
    batch_size = 500
    all_responses = []
    
    try:
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i + batch_size]
            
            messages = [
                {
                    "From": f"{FROM_NAME} <{FROM_EMAIL}>",
                    "To": email,
                    "Subject": subject,
                    "HtmlBody": html_body,
                    "TextBody": text_body,
                    "Tag": tag,
                    "TrackOpens": True,
                    "TrackLinks": "HtmlOnly"
                }
                for email in batch
            ]
            
            response = client.emails.send_batch(*messages)
            all_responses.extend(response)
            
            logger.info(f"Batch email sent: {len(batch)} recipients")
        
        return {"Messages": all_responses}
        
    except Exception as e:
        logger.error(f"Failed to send batch email: {str(e)}")
        raise