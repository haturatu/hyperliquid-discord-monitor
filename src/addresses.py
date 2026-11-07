import sys
import logging


logger = logging.getLogger("hdm.addresses")


def load_addresses(file_path: str) -> dict:
    addresses = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = [p.strip() for p in line.split(",")]
                address = parts[0].lower()

                if address:
                    tag = parts[1] if len(parts) > 1 and parts[1] else None
                    webhook = parts[2] if len(parts) > 2 and parts[2] else None
                    addresses[address] = {"tag": tag, "webhook": webhook}
    except IOError as e:
        logger.error("error reading addresses file path=%s error=%s", file_path, e)
        sys.exit(1)

    if not addresses:
        logger.error("no addresses found in addresses file path=%s", file_path)
        sys.exit(1)

    return addresses
