import re

def normalize_vessel_name(name: str) -> str:
    """Normalize a vessel name for duplicate checking by:
    - Ignoring leading and trailing spaces.
    - Ignoring multiple consecutive spaces.
    - Ignoring underscores (_).
    - Ignoring single quotes (').
    - Ignoring double quotes (").
    - Performing a case-insensitive comparison.
    
    Removing all spaces, underscores, single quotes, and double quotes and lowercasing
    satisfies all rules and makes variations like 'MV 1307', 'MV1307', "mv_1307", and '"MV 1307"' match.
    """
    if not name:
        return ""
    return name.replace(" ", "").replace("_", "").replace("'", "").replace('"', "").lower()


def normalize_folder_name(name: str) -> str:
    """Normalize a folder name for duplicate checking by:
    - Lowercasing the name
    - Retaining only alphanumeric characters (letters and digits), ignoring all spaces, quotes, and special characters
    """
    if not name:
        return ""
    return "".join(c for c in name if c.isalnum()).lower()


def clean_folder_name(name: str) -> str:
    """Clean a folder name by:
    - Replacing underscores and hyphens with spaces
    - Keeping only alphanumeric characters and spaces
    - Collapsing multiple spaces and trimming
    - Uppercasing any month names
    """
    if not name:
        return ""
    name = name.replace("_", " ").replace("-", " ")
    cleaned = "".join(c for c in name if c.isalnum() or c == " ")
    cleaned = " ".join(cleaned.split())
    
    # Uppercase month names (both full and abbreviated)
    months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
    ]
    
    for m in months:
        pattern = re.compile(rf"\b{m}\b", re.IGNORECASE)
        cleaned = pattern.sub(m.upper(), cleaned)
        
    return cleaned





