"""
Override CONFIG dict from environment variables (for Docker / VPS without editing code).
Set env vars with same key as CONFIG; lists use comma-separated values.
"""
import os


def _int_or_none(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _bool(v):
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def override_from_env(config, env_prefix=""):
    """
    Override config dict with environment variables.
    Keys are uppercased; list values use comma-separated env (e.g. COLUMN_SELLER_ID=Seller ID,Seller URL).
    """
    for key in list(config.keys()):
        env_key = (env_prefix + key).upper().replace(" ", "_")
        val = os.getenv(env_key)
        if val is None:
            continue
        val = val.strip()
        if key in ("START_FROM_LINE", "MAX_RECORDS", "WAIT_BETWEEN_REQUESTS", "API_TIMEOUT",
                   "BATCH_SIZE", "MAX_OUTPUT_TOKENS", "MAX_TOOL_CALLS", "WAIT_BETWEEN_BATCHES"):
            parsed = _int_or_none(val)
            if parsed is not None:
                config[key] = parsed
        elif key in ("USE_WEB_SEARCH",):
            config[key] = _bool(val)
        elif key in ("COLUMN_SELLER_ID", "COLUMN_SELLER_NAME", "COLUMN_PRODUCT_NAME", "COLUMN_PRODUCT_DESCRIPTION"):
            config[key] = [c.strip() for c in val.split(",") if c.strip()]
        else:
            config[key] = val
    return config
