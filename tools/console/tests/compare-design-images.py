"""Report raw RGB pixel mismatch; intentionally applies no masks."""
import json
import sys
from PIL import Image, ImageChops


def compare(left_path, right_path, diff_path):
    left = Image.open(left_path).convert("RGB")
    right = Image.open(right_path).convert("RGB")
    if left.size != right.size:
        return {"approved": list(left.size), "production": list(right.size), "comparable": False, "mismatchRatio": None}
    difference = ImageChops.difference(left, right)
    # Amplify changes so the artifact is easy to scan; no regions are masked.
    difference.point(lambda channel: min(255, channel * 4)).save(diff_path)
    # Count pixels differing in any RGB channel. This is deliberately raw:
    # any dynamic text remains visible in the evidence and contributes to the ratio.
    changed = sum(1 for pixel in difference.getdata() if pixel != (0, 0, 0))
    count = left.width * left.height
    return {"approved": list(left.size), "production": list(right.size), "comparable": True, "mismatchedPixels": changed, "totalPixels": count, "mismatchRatio": round(changed / count, 6)}


print(json.dumps({"viewport": compare(sys.argv[1], sys.argv[2], sys.argv[5]), "fullPage": compare(sys.argv[3], sys.argv[4], sys.argv[6])}))
