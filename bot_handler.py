"""
Copyright (C) 2023-2024 Fern Lane, Homeland-sees automated surveillance camera project

Licensed under the GNU Affero General Public License, Version 3.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.gnu.org/licenses/agpl-3.0.en.html

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""

import asyncio
import json
import logging
import os
import queue
import subprocess
import threading
import time
from random import choices
from string import ascii_lowercase, ascii_uppercase, digits
from typing import Any

import telegram
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

# Timeout to send document (video)
SEND_FILE_TIMEOUT = 360.0

# Maximum number of tries to send file to each user (in case of other that network error)
SEND_FILE_TRIES_MAX = 3

# After how long (in seconds) restart the bot polling if connection failed
BOT_RESTART_ON_NETWORK_ERROR = 10.0

# How long to wait before trying to send message again in case of error
DELAY_BETWEEN_RETRIES = 3.0

# How long to wait before sending message to the next user
DELAY_BETWEEN_USERS = 1.0

# Length of video unique id
VIDEO_ID_LENGTH = 12

# Bot will send rebooted_text message if this file exists on startup
REBOOT_LOCK_FILE = ".reboot"

# Bot commands
BOT_COMMAND_START = "start"
BOT_COMMAND_PAUSE = "pause"
BOT_COMMAND_TEST = "test"
BOT_COMMAND_REBOOT = "reboot"


def build_menu(
    buttons: list[InlineKeyboardButton],
    n_cols: int = 1,
    header_buttons: list[InlineKeyboardButton] | None = None,
    footer_buttons: list[InlineKeyboardButton] | None = None,
) -> list[list[Any]]:
    """Builds list of inline buttons

    Args:
        buttons (list[InlineKeyboardButton): Buttons
        n_cols (int, optional): number of columns. Defaults to 1
        header_buttons (list[InlineKeyboardButton] | None, optional): Top buttons
        footer_buttons (list[InlineKeyboardButton] | None, optional): Bottom buttons

    Returns:
        list[list[Any]]: inline buttons used to generate inlinekeyboard responses
    """
    buttons = [button for button in buttons if button is not None]
    menu = [buttons[i : i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return menu


class BotHandler:
    def __init__(self, config: dict):
        self._config = config

        self.queue = queue.Queue()
        self.test_flag = False
        self.pause_requested = False

        self._sending_thread = None
        self._sending_thread_running = False
        self._polling_thread = None
        self._application = None
        self._event_loop = None
        self._polling_stopping = False
        self._deleting_in_progress = False

    def start(self) -> None:
        """Starts internal loop and starts bot polling (non-blocking)
        Can be interrupted with KeyboardInterrupt or SystemExit
        """
        # Start internal loop
        self._start_sending_thread()

        # Start bot polling in thread
        logging.info("Starting bot polling thread")
        self._polling_thread = threading.Thread(target=self._start_polling)
        self._polling_thread.start()

        # Check for reboot file
        if os.path.exists(REBOOT_LOCK_FILE):
            try:
                with open(REBOOT_LOCK_FILE, "r", encoding="utf-8") as lock_file_io:
                    chat_id = int(lock_file_io.read().strip())
                os.remove(REBOOT_LOCK_FILE)
                asyncio.run(
                    telegram.Bot(self._config["bot_token"]).send_message(
                        chat_id=chat_id, text=self._config["rebooted_text"]
                    )
                )

            except Exception as e:
                logging.warning(f"Unable to send rebooted message: {e}")

    def stop(self) -> None:
        """Stops internal sending thread and bot polling"""
        # Set flag
        self._polling_stopping = True

        # Stop sending thread
        self._stop_sending_thread()

        # Check if we need to stop it
        if (
            self._application
            and self._application.running
            and self._polling_thread
            and self._polling_thread.is_alive()
            and self._event_loop
            and not self._event_loop.is_closed()
        ):
            try:
                # Stop polling
                logging.warning("Stopping bot polling")
                self._event_loop.stop()
                self._polling_thread.join()
                self._polling_thread = None

                # Close event loop
                logging.warning("Closing event loop")
                self._event_loop.close()
                if self._event_loop.is_closed():
                    logging.info("Event loop is closed")
                self._event_loop = None
            except Exception as e:
                logging.warning("Error stopping bot polling!", exc_info=e)

        # Clear flag (just in case)
        self._polling_stopping = False

    def _load_video_messages(self) -> dict[str, dict[str, str | list[dict[str, int]]]]:
        """Loads video_messages.json

        Returns:
            dict[str, dict[str, str | list[dict[str, int]]]]: content of video_messages.json or {}
        """
        try:
            with open(self._config["video_messages_database_file"], "r", encoding="utf-8") as json_io:
                messages: dict[str, dict[str, str | list[dict[str, int]]]] = json.load(json_io)
            return messages
        except Exception as e:
            logging.warning(f"Unable to open file {self._config['video_messages_database_file']}: {e}")
        return {}

    def _write_video_messages(self, messages: dict[str, dict[str, str | list[dict[str, int]]]]) -> None:
        """Writes video_messages.json

        Args:
            messages (dict[str, dict[str, str | list[dict[str, int]]]]): current messages database
        """
        try:
            with open(self._config["video_messages_database_file"], "w+", encoding="utf-8") as json_io:
                json.dump(messages, json_io, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.warning(f"Unable to open file {self._config['video_messages_database_file']}: {e}")

    def _sending_thread_wait(self, seconds: float) -> None:
        """time.sleep() but interrupts if self._sending_thread_running changes to False

        Args:
            seconds (float): seconds to sleep
        """
        time_started = time.time()
        while self._sending_thread_running and time.time() - time_started < seconds:
            time.sleep(0.1)

    def _sending_thread_loop(self) -> None:
        """Background thread that tries to send data no matter what
        put() video_... (where ... is filepath) or text_... (where ... is message) to send it to the user
        """
        while self._sending_thread_running:
            # Get data from queue
            data_to_send = self.queue.get(block=True)

            # Exit from loop if None
            if data_to_send is None:
                break

            # Read current database
            messages = self._load_video_messages()
            video_id = None

            # Extract actual data
            # video_...
            if data_to_send.startswith("video_"):
                data_to_send = data_to_send[6:]

                # Generate random id
                while not video_id or video_id in messages:
                    video_id = "".join(choices(ascii_uppercase + ascii_lowercase + digits, k=VIDEO_ID_LENGTH))

                # Write data
                messages[video_id] = {"path": data_to_send, "messages": []}
                self._write_video_messages(messages)

                # Send video message to each user sequentially
                for user_id in self._config["users_whitelist_video"]:
                    # Exit?
                    if not self._sending_thread_running:
                        break

                    # Send
                    self._send_text_or_video(True, user_id, data_to_send, video_id, messages)

            # text_...
            elif data_to_send.startswith("text_"):
                data_to_send = data_to_send[5:]

                # Send text message to each user sequentially
                for user_id in self._config["users_whitelist_text"]:
                    # Exit?
                    if not self._sending_thread_running:
                        break

                    # Send
                    self._send_text_or_video(False, user_id, data_to_send, video_id, messages)

            # Just in case...
            else:
                logging.error(f"Unknown data type to send: {data_to_send}")
                return

        # Loop finished
        logging.warning("sending_thread loop finished")

    def _send_text_or_video(
        self,
        video: bool,
        user_id: int,
        data_to_send: str,
        video_id: str | None,
        video_msgs: dict[str, dict[str, str | list[dict[str, int]]]],
    ) -> None:
        """Tries to send text or video message to the user multiple times in case of error

        Args:
            video (bool): False for text, True for video
            user_id (int): chat ID
            data_to_send (str): trimmed message or video path
            video_id (str | None): unique video ID
            video_msgs (dict[str, dict[str, str  |  list[dict[str, int]]]]): database from _load_video_messages()
        """
        # Log
        logging.info(f"Trying to send {'video' if video else 'message'} to {user_id}")

        # Try to send multiple times
        tries_counter = 0
        while self._sending_thread_running:
            try:
                # Send video
                if video:
                    # Create delete button
                    button_delete = InlineKeyboardButton(
                        self._config["delete_local_file_btn_text"],
                        callback_data=f"ask_{video_id}",
                    )
                    reply_markup = InlineKeyboardMarkup(build_menu([button_delete]))

                    # Extract filename
                    filename = os.path.basename(data_to_send)

                    # Try to send message and get message ID
                    message_id = (
                        asyncio.run(
                            telegram.Bot(self._config["bot_token"]).send_video(
                                chat_id=user_id,
                                video=data_to_send,
                                filename=filename,
                                supports_streaming=True,
                                read_timeout=SEND_FILE_TIMEOUT,
                                write_timeout=SEND_FILE_TIMEOUT,
                                pool_timeout=SEND_FILE_TIMEOUT,
                                caption=filename,
                                reply_markup=reply_markup,
                            )
                        )
                    ).message_id

                    # Check and add to the database
                    if message_id >= 0:
                        try:
                            logging.info("Done. Adding video message to the database")

                            # Append
                            messages_list: list[dict[str, int]] = video_msgs[video_id]["messages"]  # pyright: ignore
                            messages_list.append({"chat_id": user_id, "message_id": message_id})

                            # Save database
                            self._write_video_messages(video_msgs)
                        except Exception as e_:
                            logging.error(
                                "Error adding message to the database!",
                                exc_info=e_,
                            )

                # Send plain message
                else:
                    message_id = (
                        asyncio.run(
                            telegram.Bot(self._config["bot_token"]).send_message(
                                chat_id=user_id,
                                text=data_to_send,
                                disable_web_page_preview=True,
                            )
                        )
                    ).message_id

                # Check and exit from loop
                if message_id >= 0:
                    break
                else:
                    raise Exception(f"Wrong message ID: {message_id}")

            # Error
            except Exception as e:
                # Networking error
                if e.__class__ == telegram.error.NetworkError:
                    logging.warning("NetworkError while trying to send data!")

                    # Reset number of tries
                    tries_counter = 1

                # Other error
                else:
                    logging.error("Error sending data!", exc_info=e)

                    # Increment tries counter
                    tries_counter += 1

            # Check retries and exit from loop
            if tries_counter >= SEND_FILE_TRIES_MAX:
                logging.error(f"{SEND_FILE_TRIES_MAX} retries exceeded!")
                break

            # Wait some time before trying again
            logging.warning(f"Retrying after {DELAY_BETWEEN_RETRIES:.2f}")
            self._sending_thread_wait(DELAY_BETWEEN_RETRIES)

        # Wait before next user
        self._sending_thread_wait(DELAY_BETWEEN_USERS)

    def _start_sending_thread(self) -> None:
        """Starts internal thread to send messages and videos"""
        # Nothing to do
        if self._sending_thread is not None:
            return

        # Start thread
        logging.info("Starting sending_thread loop")
        self._sending_thread_running = True
        self._sending_thread = threading.Thread(target=self._sending_thread_loop)
        self._sending_thread.start()

    def _stop_sending_thread(self) -> None:
        """Stops internal thread to send messages and videos"""
        # Nothing to do
        if self._sending_thread is None:
            return

        # Stop thread and wait for it
        logging.info("Stopping sending_thread loop")
        self.queue.put(None)
        self._sending_thread_running = False
        try:
            self._sending_thread.join()
        except Exception as e:
            logging.warning(f"Error joining sending_thread loop: {e}")

        # Done
        self._sending_thread = None

    async def _send_message_safe(self, chat_id: int, text: str) -> int | None:
        """Sends message without raising any error

        Args:
            chat_id (int): user id
            text (str): message

        Returns:
            int | None: message_id if sent successfully or None if not
        """
        try:
            return (await telegram.Bot(self._config["bot_token"]).send_message(chat_id=chat_id, text=text)).message_id
        except Exception as e:
            logging.error(f"Error sending message {text} to {chat_id}!", exc_info=e)
        return None

    async def _command_start(self, update: Update, _) -> None:
        """Handles /start command

        Args:
            update (Update): Update instance
        """
        # Get user id
        chat_id = update.effective_chat.id
        logging.info(f"/start command from {chat_id}")

        # Check whitelist
        if chat_id not in self._config["users_whitelist_text"]:
            logging.warning(f"User {chat_id} is not whitelisted!")
            await self._send_message_safe(
                chat_id,
                self._config["start_text_not_in_whitelist"].format(chat_id=chat_id),
            )
            return

        # Resume event handler
        self.pause_requested = False

        # Notify all users
        await self._send_message_safe(chat_id, self._config["start_text"])
        for chat_id_ in self._config["users_whitelist_text"]:
            if chat_id_ != chat_id:
                time.sleep(DELAY_BETWEEN_USERS)
                await self._send_message_safe(chat_id_, self._config["start_text"])

    async def _command_pause(self, update: Update, _) -> None:
        """Handles /pause command

        Args:
            update (Update): Update instance
        """
        # Get user id
        chat_id = update.effective_chat.id
        logging.info(f"/pause command from {chat_id}")

        # Check whitelist
        if chat_id not in self._config["users_whitelist_text"]:
            logging.warning(f"User {chat_id} is not whitelisted!")
            return

        # Request pause
        self.pause_requested = True

        # Notify all users
        await self._send_message_safe(chat_id, self._config["pause_text"])
        for chat_id_ in self._config["users_whitelist_text"]:
            if chat_id_ != chat_id:
                time.sleep(DELAY_BETWEEN_USERS)
                await self._send_message_safe(chat_id_, self._config["pause_text"])

    async def _command_test(self, update: Update, _) -> None:
        """Handles /test command

        Args:
            update (Update): Update instance
        """
        # Get user id
        chat_id = update.effective_chat.id
        logging.info(f"/test command from {chat_id}")

        # Check whitelist
        if chat_id not in self._config["users_whitelist_text"]:
            logging.warning(f"User {chat_id} is not whitelisted!")
            return

        # Set flag
        self.test_flag = True

    async def _command_reboot(self, update: Update, _) -> None:
        """Handles /reboot command

        Args:
            update (Update): Update instance
        """
        # Get user id
        chat_id = update.effective_chat.id
        logging.info(f"/reboot command from {chat_id}")

        # Check whitelist
        if chat_id not in self._config["users_whitelist_text"]:
            logging.warning(f"User {chat_id} is not whitelisted!")
            return

        # Create lock file and write chat id into it
        with open(REBOOT_LOCK_FILE, "w+", encoding="utf-8") as lock_file_io:
            lock_file_io.write(str(chat_id))

        # Send rebooting message
        await self._send_message_safe(chat_id, self._config["rebooting_text"])

        # Reboot
        subprocess.run(
            self._config["reboot_command"], shell=isinstance(self._config["reboot_command"], str), check=False
        )

    async def _query_callback(self, update: Update, _) -> None:
        """reply_markup buttons callback

        Args:
            update (Update): Update instance
        """
        try:
            # Get chat ID, message ID and query data
            chat_id = update.effective_chat.id
            if update.message:
                message_id = update.message.id
            else:
                message_id = update.effective_message.id
            query_data = update.callback_query.data
        except Exception as e:
            logging.error("Error retrieving chat_id or message_id!", exc_info=e)
            return

        # Log
        logging.info(f"Button pressed from user {chat_id} on message ID: {message_id}")

        # Check chat ID and message ID
        if chat_id is None or message_id is None or not query_data:
            return

        # Check whitelist
        if chat_id not in self._config["users_whitelist_text"]:
            logging.warning(f"User {chat_id} is not whitelisted!")
            return

        # Read video messages
        messages = self._load_video_messages()

        # Parse video id
        if query_data.startswith("ask_"):
            video_id = query_data[4:]
        elif query_data.startswith("keep_"):
            video_id = query_data[5:]
        elif query_data.startswith("delete_"):
            video_id = query_data[7:]
        else:
            logging.error(f"Unknown command: {query_data}")
            return

        # Check if already deleted
        if video_id not in messages:
            logging.warning(f"Video with ID {video_id} already deleted")
            await self._send_deleted(update, video_id, chat_id, message_id)
            return

        # Extract path
        filepath: str = messages[video_id]["path"]  # pyright: ignore[reportAssignmentType]

        # Ask for delete
        if query_data.startswith("ask_"):
            video_id = query_data[4:]
            button_no = InlineKeyboardButton(self._config["no_btn_text"], callback_data=f"keep_{video_id}")
            button_yes = InlineKeyboardButton(self._config["yes_btn_text"], callback_data=f"delete_{video_id}")
            reply_markup = InlineKeyboardMarkup(build_menu([button_no, button_yes], n_cols=2))
            try:
                filename = os.path.basename(filepath)
                await update.get_bot().edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=self._config["are_you_sure_text"].format(filename=filename),
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logging.error("Error editing message caption!", exc_info=e)

        # Keep file
        elif query_data.startswith("keep_"):
            video_id = query_data[5:]
            button_delete = InlineKeyboardButton(
                self._config["delete_local_file_btn_text"],
                callback_data=f"ask_{video_id}",
            )
            reply_markup = InlineKeyboardMarkup(build_menu([button_delete]))
            try:
                await update.get_bot().edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=os.path.basename(filepath),
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logging.error("Error editing message caption!", exc_info=e)

        # Delete file
        elif query_data.startswith("delete_"):
            # Prevent multiple delete actions at the same time
            if self._deleting_in_progress:
                logging.warning("Waiting for previous deleting action to finish")
                while self._deleting_in_progress:
                    time.sleep(0.01)

            # Set lock
            self._deleting_in_progress = True

            # Already deleted
            if not os.path.exists(filepath):
                logging.info(f"File {filepath} not exists, so nothing to delete!")
                await self._send_deleted(update, video_id, chat_id, message_id)

            # Delete file
            else:
                try:
                    logging.warning(f"Deleting {filepath} file")
                    os.remove(filepath)
                    await self._send_deleted(update, video_id, chat_id, message_id)
                except Exception as e:
                    logging.error(f"Error deleting file: {filepath}", exc_info=e)

            # Release lock
            self._deleting_in_progress = False

    async def _send_deleted(self, update: Update, video_id: str | None, chat_id: int, message_id: int) -> None:
        """Sends "local file deleted" message to all users

        Args:
            update (Update): Update instance
            video_id (str | None): ID of video if found in video_messages.json
            chat_id (int): user id
            message_id (int): video message
        """
        # Send to requested user
        try:
            await update.get_bot().edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=self._config["local_file_deleted_text"],
                reply_markup=None,
            )
        except Exception as e_:
            logging.error("Error editing message caption!", exc_info=e_)

        # Read current database
        messages = self._load_video_messages()

        # Check
        if video_id not in messages:
            return

        # Edit captions for all messages
        messages_list: list[dict[str, int]] = messages[video_id]["messages"]  # pyright: ignore[reportAssignmentType]
        for message_ in messages_list:
            try:
                chat_id_ = message_["chat_id"]
                message_id_ = message_["message_id"]
                if chat_id_ != chat_id or message_id_ != message_id:
                    time.sleep(DELAY_BETWEEN_USERS)
                    logging.info(f"Editing caption on message {message_id_} (user: {chat_id_})")
                    await update.get_bot().edit_message_caption(
                        chat_id=chat_id_,
                        message_id=message_id_,
                        caption=self._config["local_file_deleted_text"],
                        reply_markup=None,
                    )
            except Exception as e_:
                logging.error("Error editing message caption!", exc_info=e_)

        # Delete from database
        del messages[video_id]
        self._write_video_messages(messages)

    def _start_polling(self) -> None:
        """Background thread that sets commands description and starts bot polling"""
        # Check and create new event loop
        if self._event_loop is None:
            logging.info("Creating new event loop")
            self._event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._event_loop)

        # Build bot
        builder = ApplicationBuilder().token(self._config["bot_token"])
        self._application = builder.build()

        # Add handlers
        self._application.add_handler(CommandHandler(BOT_COMMAND_START, self._command_start))
        self._application.add_handler(CommandHandler(BOT_COMMAND_PAUSE, self._command_pause))
        self._application.add_handler(CommandHandler(BOT_COMMAND_TEST, self._command_test))
        self._application.add_handler(CommandHandler(BOT_COMMAND_REBOOT, self._command_reboot))
        self._application.add_handler(CallbackQueryHandler(self._query_callback))

        # Set commands description
        try:
            logging.info("Trying to set bot commands")
            self._event_loop.run_until_complete(
                self._application.bot.set_my_commands(
                    [
                        BotCommand(BOT_COMMAND_START, self._config["command_start_description"]),
                        BotCommand(BOT_COMMAND_PAUSE, self._config["command_pause_description"]),
                        BotCommand(BOT_COMMAND_TEST, self._config["command_test_description"]),
                        BotCommand(BOT_COMMAND_REBOOT, self._config["command_reboot_description"]),
                    ]
                )
            )
        except Exception as e:
            logging.error("Error setting bot commands description!", exc_info=e)

        # Start polling
        logging.info("Starting bot polling")
        while True:
            try:
                # Try to start
                self._application.run_polling(close_loop=False, stop_signals=[])

                # If we are here, event loop is closed because run_polling() is blocking
                break

            # Couldn't connect -> restart bot
            except telegram.error.NetworkError:
                if self._polling_stopping:
                    break
                logging.warning(f"NetworkError. Restarting bot after {BOT_RESTART_ON_NETWORK_ERROR:.2f}s")
                time.sleep(BOT_RESTART_ON_NETWORK_ERROR)

            # Bot error?
            except Exception as e:
                if self._polling_stopping:
                    break
                logging.error("Error starting bot polling!", exc_info=e)
                break

        # Bot stopped
        logging.warning("Bot polling thread finished")
