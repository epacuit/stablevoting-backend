import qrcode
from io import BytesIO
import base64
from typing import Optional


def generate_poll_qr_code(
    poll_url: str,
    size: int = 10,
    border: int = 4,
    dark_color: str = "#000000",
    light_color: str = "#FFFFFF"
) -> dict:
    """Generate a QR code for a poll URL"""
    
    # Create QR code instance
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=size,
        border=border,
    )
    
    # Add data and optimize
    qr.add_data(poll_url)
    qr.make(fit=True)
    
    # Create the image
    img = qr.make_image(fill_color=dark_color, back_color=light_color)
    
    # Convert to bytes
    img_bytes = BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    
    # Convert to base64 for easy embedding
    img_base64 = base64.b64encode(img_bytes.getvalue()).decode()
    
    return {
        "image_base64": img_base64,
        "image_bytes": img_bytes.getvalue(),
        "data_url": f"data:image/png;base64,{img_base64}"
    }


def generate_embed_code(
    poll_id: str,
    base_url: str = "https://stablevoting.org",
    width: str = "100%",
    height: str = "600px",
    title: Optional[str] = None
) -> dict:
    """Generate HTML embed code for a poll"""
    
    embed_url = f"{base_url}/embed/{poll_id}"
    vote_url = f"{base_url}/vote/{poll_id}"
    
    # Basic iframe embed
    iframe_code = f'''<iframe 
    src="{embed_url}"
    width="{width}"
    height="{height}"
    frameborder="0"
    title="{title or 'Stable Voting Poll'}"
    style="border: 1px solid #ccc; border-radius: 8px;">
</iframe>'''
    
    return {
        "embed_url": embed_url,
        "vote_url": vote_url,
        "iframe_code": iframe_code
    }