"""
Override CONFIG dict from environment variables (for Docker / VPS without editing code).
Set env vars with same key as CONFIG; lists use comma-separated values.

Amazon reseller pipeline (amazon_resellers_pipeline.py): uses a configurable prefix so keys do not
clash with main.py / gpt.py. Default prefix is RESELLER_ (e.g. RESELLER_INPUT_CSV). Override the
prefix itself with RESELLER_CONFIG_PREFIX in .env (e.g. RESELLER_CONFIG_PREFIX=MyApp → MyApp_INPUT_CSV).
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

    Only overrides when the env var is present in .env (and not commented). If the var is missing
    or commented out, CONFIG keeps its default from the code. If the var is set to none/null/empty,
    we set that config key to None (e.g. START_FROM_LINE=none → start from beginning).
    """
    for key in list(config.keys()):
        env_key = (env_prefix + key).upper().replace(" ", "_")
        val = os.getenv(env_key)
        # Not in .env or commented out → do not update; keep value from code
        if val is None:
            continue
        val = val.strip()
        if key in ("START_FROM_LINE", "MAX_RECORDS", "WAIT_BETWEEN_REQUESTS", "API_TIMEOUT",
                   "BATCH_SIZE", "MAX_OUTPUT_TOKENS", "MAX_TOOL_CALLS", "WAIT_BETWEEN_BATCHES",
                   "STAGE1_WRITE_BATCH_SIZE", "STAGE2_WRITE_BATCH_SIZE", "STAGE3_BATCH_SIZE",
                   "STAGE3_WRITE_BATCH_SIZE", "STAGE4_BATCH_SIZE", "STAGE5_WRITE_BATCH_SIZE",
                   "WRITE_BATCH_SIZE",
                   "STAGE2_WORKERS", "STAGE3_WORKERS", "STAGE4_WORKERS", "STAGE5_WORKERS",
                   "API_HTTP_RETRIES", "GPT_HTTP_RETRIES", "APOLLO_RATE_LIMIT_PER_MIN",
                   "APOLLO_API_TIMEOUT", "APOLLO_HTTP_RETRIES", "SEARCH_MAX_RESULTS"):
            # Var is present and set to none/null/empty → set config to None (e.g. no start line limit)
            if val.lower() in ("none", "null", ""):
                config[key] = None
            else:
                parsed = _int_or_none(val)
                if parsed is not None:
                    config[key] = parsed
        elif key in ("MIN_RATING_PREFERRED",):
            if val.lower() in ("none", "null", ""):
                config[key] = None
            else:
                try:
                    config[key] = float(val.replace(",", "."))
                except (ValueError, TypeError):
                    pass
        elif key in (
            "USE_WEB_SEARCH",
            "RUN_STAGE2_PERSON_STAGE",
            "APOLLO_SEARCH_BY_NAME_FALLBACK",
            "SKIP_EXISTING",
            "REVEAL_PHONE_NUMBER",
        ):
            config[key] = _bool(val)
        elif key in (
            "COLUMN_SELLER_ID",
            "COLUMN_SELLER_NAME",
            "COLUMN_PRODUCT_NAME",
            "COLUMN_PRODUCT_DESCRIPTION",
            "COLUMN_COMPANY_NAME",
            "COLUMN_WEBSITE",
            "COLUMN_EMAIL",
            "COLUMN_ASIN_OR_URL",
            "COLUMN_SELLER_COUNT",
            "COLUMN_BRAND_NAME",
        ):
            config[key] = [c.strip() for c in val.split(",") if c.strip()]
        else:
            config[key] = val
    return config
