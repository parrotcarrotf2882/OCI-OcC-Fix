"""
OCI Out of Capacity Fix
Version v2.2.0
Moses (@mosesman831)
GitHub: https://github.com/mosesman831/OCI-OcC-Fix
"""

import argparse
import os
import oci
import logging
import time
import sys
import telebot
import datetime
import configparser
import json
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, List

# Constants
CONFIG_FILE = 'configuration.ini'
OCI_CONFIG_FILE = 'config'
LOG_FILE = 'oci_occ.log'
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3
RETRYABLE_ERROR_CODES = {"TooManyRequests", "OutOfHostCapacity", "OutOfCapacity"}
ATTEMPT_COUNT_FILE = Path(os.environ.get("ATTEMPT_COUNT_FILE", "attempt_count.txt"))

class OciOccFix:
    def __init__(self, config_path: Path, oci_config_path: Path):
        self.config_path = config_path
        self.oci_config_path = oci_config_path
        self.attempt_count_path = ATTEMPT_COUNT_FILE

        # Phase 1: Core configuration
        self.config = self.load_config(self.config_path)
        self.setup_logging()

        # Phase 2: Retry timing
        self.wait_seconds = self.config.getint(
            'Retry',
            'initial_retry_interval',
            fallback=20
        )

        # Phase 3: OCI clients
        self.clients = self.initialize_oci_clients()

        # Phase 4: Persistent runtime state
        self.total_retries = self.load_attempt_count()
        self.retry_counter = 0

        # Phase 5: Telegram integration
        self.tg_message_id = None
        self.tg_bot = self.initialize_telegram()

    @staticmethod
    def _parse_nonnegative_int(value: object) -> Optional[int]:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return max(0, parsed)

    def load_attempt_count(self) -> int:
        """Load the largest valid counter from the environment or state file."""
        candidates: List[int] = []

        env_value = self._parse_nonnegative_int(
            os.environ.get("PREV_ATTEMPTS")
        )
        if env_value is not None:
            candidates.append(env_value)

        try:
            if self.attempt_count_path.exists():
                file_value = self._parse_nonnegative_int(
                    self.attempt_count_path.read_text(
                        encoding="utf-8"
                    )
                )
                if file_value is not None:
                    candidates.append(file_value)
        except OSError as exc:
            logging.warning(f"Could not read attempt counter: {exc}")

        count = max(candidates, default=0)
        logging.info(f"Loaded previous failed attempts: {count}")
        return count

    def persist_attempt_count(self) -> None:
        """Atomically save the failed-attempt counter for the workflow."""
        temp_path = self.attempt_count_path.with_suffix(
            self.attempt_count_path.suffix + ".tmp"
        )
        try:
            temp_path.write_text(
                str(self.total_retries) + "\n",
                encoding="utf-8"
            )
            temp_path.replace(self.attempt_count_path)
        except OSError as exc:
            logging.warning(f"Could not save attempt counter: {exc}")

    @staticmethod
    def load_config(config_path: Path) -> configparser.ConfigParser:
        """Load and validate configuration with strict checks"""
        config = configparser.ConfigParser()
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file {config_path} not found")
        
        config.read(config_path)
        
        # Validate required sections
        required_sections = ['OCI', 'Instance', 'Telegram', 'Machine', 'Retry']
        for section in required_sections:
            if not config.has_section(section):
                raise ValueError(f"Missing required section: [{section}]")

        # Validate Retry parameters
        required_retry_keys = [
            'min_interval',
            'max_interval',
            'initial_retry_interval',
            'backoff_factor'
        ]
        for key in required_retry_keys:
            if not config.has_option('Retry', key):
                raise ValueError(f"Missing required Retry key: {key}")
                
        return config

    def setup_logging(self):
        """Configure logging with rotation and validation"""
        formatter = logging.Formatter('[%(levelname)s] %(asctime)s - %(message)s')
        log_level = self.config.get(
            'Logging', 
            'log_level', 
            fallback='INFO'
        ).upper()

        handlers = [
            RotatingFileHandler(
                LOG_FILE,
                maxBytes=MAX_LOG_SIZE,
                backupCount=LOG_BACKUP_COUNT,
                encoding='utf-8'
            ),
            logging.StreamHandler()
        ]

        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format=formatter._fmt,
            handlers=handlers
        )

    def initialize_oci_clients(self) -> Dict[str, object]:
        """Initialize OCI clients with error containment"""
        try:
            self.oci_config = oci.config.from_file(str(self.oci_config_path))
            return {
                'compute': oci.core.ComputeClient(self.oci_config),
                'identity': oci.identity.IdentityClient(self.oci_config),
                'network': oci.core.VirtualNetworkClient(self.oci_config),
                'blockstorage': oci.core.BlockstorageClient(self.oci_config)
            }
        except Exception as e:
            logging.error(f"OCI client initialization failed: {str(e)}")
            sys.exit(1)

    def initialize_telegram(self) -> Optional[telebot.TeleBot]:
        """Initialize Telegram bot with safe defaults"""
        bot_token = self.config.get('Telegram', 'bot_token', fallback='')
        uid = self.config.get('Telegram', 'uid', fallback='')
        
        if not bot_token or bot_token == 'xxxx':
            return None
        if not uid or uid == 'xxxx':
            return None
            
        try:
            bot = telebot.TeleBot(bot_token)
            self.send_telegram_startup_message(bot)
            return bot
        except Exception as e:
            logging.warning(f"Telegram initialization failed: {str(e)}")
            return None

    def send_telegram_startup_message(self, bot: telebot.TeleBot):
        """Send startup message with enhanced error handling"""
        try:
            tenancy = self.clients['identity'].get_tenancy(
                self.oci_config['tenancy']
            ).data
            users = self.clients['identity'].list_users(
                compartment_id=self.oci_config['tenancy']
            ).data
            
            message = (
                "🚀 OCI-OcC-Fix Initialized\n"
                f"• Account: {tenancy.name}\n"
                f"• User: {users[0].email if users else 'Unknown'}\n"
                f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"• Retry Interval: {self.wait_seconds}s\n"
                f"• Previous failed attempts: {self.total_retries}\n"
                f"• Machine: {self.config.get('Machine', 'shape')}"
            )
            
            sent = bot.send_message(self.config.get('Telegram', 'uid'), message)
            self.tg_message_id = sent.message_id
        except Exception as e:
            logging.error(f"Telegram startup message failed: {str(e)}")

    def validate_resources(self) -> bool:
        """Perform comprehensive resource validation with error handling"""
        try:
            compartment_id = self.config.get('OCI', 'compartment_id')
            total_storage = 0

            # Storage validation
            volumes = self.clients['blockstorage'].list_volumes(compartment_id=compartment_id).data
            total_storage += sum(
                v.size_in_gbs 
                for v in volumes 
                if v.lifecycle_state not in ("TERMINATING", "TERMINATED")
            )

            # Boot volumes check
            ads = json.loads(self.config.get('OCI', 'availability_domains'))
            for ad in ads:
                boot_volumes = self.clients['blockstorage'].list_boot_volumes(
                    compartment_id=compartment_id,
                    availability_domain=ad.strip()
                ).data
                total_storage += sum(
                    bv.size_in_gbs 
                    for bv in boot_volumes 
                    if bv.lifecycle_state not in ("TERMINATING", "TERMINATED")
                )

            required_size = self.config.getint(
                'Instance', 
                'boot_volume_size', 
                fallback=50
            )
            if (200 - total_storage) < required_size:
                logging.critical(
                    f"Storage limit exceeded: {200 - total_storage}GB free < {required_size}GB needed"
                )
                return False

            # Instance validation
            instances = self.clients['compute'].list_instances(compartment_id=compartment_id).data
            active_instances = [
                i for i in instances 
                if i.lifecycle_state not in ("TERMINATING", "TERMINATED")
            ]
            
            if self.config.get('Instance', 'display_name') in [i.display_name for i in active_instances]:
                logging.critical("Duplicate instance name detected")
                return False

            # ARM quota validation
            if self.config.get('Machine', 'type').upper() == 'ARM':
                arm_instances = [
                    i for i in active_instances 
                    if i.shape == "VM.Standard.A1.Flex"
                ]
                total_ocpus = sum(i.shape_config.ocpus for i in arm_instances)
                total_memory = sum(i.shape_config.memory_in_gbs for i in arm_instances)
                
                new_ocpus = self.config.getint('Machine', 'ocpus')
                new_memory = self.config.getint('Machine', 'memory')
                
                if (total_ocpus + new_ocpus) > 4 or (total_memory + new_memory) > 24:
                    logging.critical("ARM quota exceeded: Max 4 OCPUs/24GB")
                    return False

            return True

        except Exception as e:
            logging.error(f"Resource validation failed: {str(e)}")
            return False

    def create_instance(self, availability_domain: str) -> Optional[str]:
        """Create instance with robust error handling"""
        try:
            launch_details = oci.core.models.LaunchInstanceDetails(
                metadata={
                    "ssh_authorized_keys": self.config.get('Instance', 'ssh_keys')
                },
                availability_domain=availability_domain.strip(),
                compartment_id=self.config.get('OCI', 'compartment_id'),
                shape=self.config.get('Machine', 'shape'),
                display_name=self.config.get('Instance', 'display_name'),
                source_details=self.get_source_details(),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=self.config.get('OCI', 'subnet_id'),
                    assign_public_ip=True
                ),
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.config.getint('Machine', 'ocpus'),
                    memory_in_gbs=self.config.getint('Machine', 'memory')
                )
            )

            response = self.clients['compute'].launch_instance(launch_details)
            return response.data.id
        except oci.exceptions.ServiceError as e:
            error_code = e.code
            error_message = str(e.message or "")
            error_code_display = error_code or 'UnknownServiceError'
            logging.warning(
                f"Create failed in {availability_domain}: "
                f"{error_code_display} - {error_message}"
            )

            normalized_error = error_code
            lowered_message = error_message.lower()

            # OCI sometimes returns capacity exhaustion as InternalError
            # while the useful reason is only present in the message text.
            if (
                "out of host capacity" in lowered_message
                or "out of capacity" in lowered_message
            ):
                normalized_error = "OutOfHostCapacity"

            if normalized_error in RETRYABLE_ERROR_CODES:
                self.adaptive_retry_wait(normalized_error)

            return None
        except Exception as e:
            logging.error(f"Unexpected creation error: {str(e)}")
            return None

    def get_source_details(self):
        """Get source config with fallback handling"""
        if self.config.get('OCI', 'boot_volume_id', fallback='xxxx') != 'xxxx':
            return oci.core.models.InstanceSourceViaBootVolumeDetails(
                source_type="bootVolume",
                boot_volume_id=self.config.get('OCI', 'boot_volume_id')
            )
        
        return oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=self.config.get('OCI', 'image_id'),
            boot_volume_size_in_gbs=self.config.getint(
                'Instance', 
                'boot_volume_size',
                fallback=50
            )
        )

    def get_public_ip_with_retry(
        self,
        instance_id: str,
        attempts: int = 18,
        delay_seconds: int = 5
    ) -> str:
        """Wait for the VNIC and public IP to become available."""
        for attempt in range(1, attempts + 1):
            try:
                attachments = self.clients['compute'].list_vnic_attachments(
                    compartment_id=self.config.get('OCI', 'compartment_id'),
                    instance_id=instance_id
                ).data

                if not attachments:
                    raise RuntimeError("VNIC attachment is not ready")

                private_ips = self.clients['network'].list_private_ips(
                    vnic_id=attachments[0].vnic_id
                ).data

                if not private_ips:
                    raise RuntimeError("Private IP is not ready")

                public_ip = self.clients['network'].get_public_ip_by_private_ip_id(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(
                        private_ip_id=private_ips[0].id
                    )
                ).data.ip_address

                if public_ip:
                    return public_ip

            except Exception as exc:
                logging.info(
                    f"Waiting for public IP ({attempt}/{attempts}): {exc}"
                )

            if attempt < attempts:
                time.sleep(delay_seconds)

        return "pending"

    def send_telegram_success(self, message: str) -> None:
        """Send a separate success message with retries."""
        if not self.tg_bot:
            return

        chat_id = self.config.get('Telegram', 'uid')
        last_error = None

        for attempt in range(1, 4):
            try:
                self.tg_bot.send_message(chat_id, message)
                logging.info("Telegram success notification sent")
                return
            except Exception as exc:
                last_error = exc
                logging.warning(
                    f"Telegram success notification attempt "
                    f"{attempt}/3 failed: {exc}"
                )
                if attempt < 3:
                    time.sleep(5)

        logging.error(
            f"Telegram success notification completely failed: {last_error}"
        )

    def handle_success(self, instance_id: str):
        """Persist success immediately, retrieve IP, then notify Telegram."""
        Path("instance_created.flag").write_text(
            instance_id + "\n",
            encoding="utf-8"
        )
        self.persist_attempt_count()

        public_ip = self.get_public_ip_with_retry(instance_id)

        logging.info(f"✅ Instance created! Public IP: {public_ip}")
        self.send_telegram_success(
            "🚀 Instance Ready!\n"
            f"• IP: {public_ip}\n"
            f"• Failed attempts before success: {self.total_retries}\n"
            f"• Successful attempt number: {self.total_retries + 1}\n"
            f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            "• Disable the GitHub Actions workflow now."
        )
        sys.exit(0)

    def send_telegram_update(self, message: str):
        """Update the current Telegram status message."""
        if not self.tg_bot or not self.tg_message_id:
            return

        try:
            self.tg_bot.edit_message_text(
                chat_id=self.config.get('Telegram', 'uid'),
                message_id=self.tg_message_id,
                text=message
            )
        except Exception as exc:
            logging.warning(f"Telegram update failed: {exc}")

    def adaptive_retry_wait(self, error_code: str):
        """Adjust retry timing with bounds checking"""
        min_interval = self.config.getint('Retry', 'min_interval', fallback=1)
        max_interval = self.config.getint('Retry', 'max_interval', fallback=60)
        backoff_factor = self.config.getfloat('Retry', 'backoff_factor', fallback=1.5)

        if error_code == 'TooManyRequests':
            self.wait_seconds = min(
                self.wait_seconds * backoff_factor,
                max_interval
            )
        else:
            self.wait_seconds = max(
                self.wait_seconds / 1.5,
                min_interval
            )

        # Ensure wait stays within configured bounds
        self.wait_seconds = max(min(self.wait_seconds, max_interval), min_interval)
        logging.info(f"⏳ Next retry in {self.wait_seconds:.1f}s")

    def run(self):
        """Main execution loop with a persistent failed-attempt counter."""
        if not self.validate_resources():
            logging.critical("❌ Resource validation failed")
            sys.exit(1)

        ads = json.loads(self.config.get('OCI', 'availability_domains'))

        while True:
            try:
                for ad in ads:
                    attempt_number = self.total_retries + 1
                    logging.info(
                        f"Trying attempt {attempt_number} in {ad}"
                    )

                    instance_id = self.create_instance(ad)

                    if instance_id:
                        self.handle_success(instance_id)

                    # Only failed launch requests are added to the counter.
                    self.total_retries += 1
                    self.persist_attempt_count()

                    if self.total_retries % 10 == 0 and self.tg_bot:
                        self.send_telegram_update(
                            f"🔁 Attempt {self.total_retries}\n"
                            f"• Last Error: {ad} capacity\n"
                            f"• Next retry: {self.wait_seconds:.1f}s"
                        )

                    time.sleep(self.wait_seconds)

            except KeyboardInterrupt:
                self.persist_attempt_count()
                logging.info("🛑 Process interrupted by user")
                self.send_telegram_update("🛑 Process interrupted by user")
                sys.exit(0)

            except Exception as exc:
                self.persist_attempt_count()
                error_code = getattr(exc, 'code', 'Unknown')
                logging.error(f"⚠️ Unexpected error: {exc}")
                self.adaptive_retry_wait(error_code)
                time.sleep(self.wait_seconds)

def main() -> None:
    parser = argparse.ArgumentParser(description="OCI-OcC-Fix runner")
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        help="Path to configuration.ini",
    )
    parser.add_argument(
        "--oci-config",
        default=OCI_CONFIG_FILE,
        help="Path to OCI SDK config file (default: ./config)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    oci_config_path = Path(args.oci_config).expanduser().resolve()

    try:
        OciOccFix(config_path=config_path, oci_config_path=oci_config_path).run()
    except Exception as e:
        logging.critical(f"💀 Fatal initialization error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
