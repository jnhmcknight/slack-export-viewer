import logging
import re
import sys

import emoji
import markdown2

from slackviewer.user import User

# Workaround for ASCII encoding error in Python 2.7
# See https://github.com/hfaran/slack-export-viewer/issues/81
if sys.version_info[0] == 2:
    reload(sys)
    sys.setdefaultencoding('utf8')

class SlackFormatter(object):
    "This formats messages and provides access to workspace-wide data (user and channel metadata)."

    # Class-level constants for precompilation of frequently-reused regular expressions
    # URL detection relies on http://stackoverflow.com/a/1547940/1798683
    _LINK_PAT = re.compile(r"<(https|http|mailto):[A-Za-z0-9_\.\-\/\?\,\=\#\:\@]+\|[^>]+>")
    _MENTION_PAT = re.compile(r"<((?:#C|@[UB])\w+)(?:\|([A-Za-z0-9.-_]+))?>")
    _HASHTAG_PAT = re.compile(r"(^| )#[A-Za-z][\w\.\-\_]+( |$)")

    def __init__(self, USER_DATA, CHANNEL_DATA):
        self.__USER_DATA = USER_DATA
        self.__CHANNEL_DATA = CHANNEL_DATA

    def find_user(self, message):
        if message.get("subtype", "").startswith("bot_") and "bot_id" in message and message["bot_id"] not in self.__USER_DATA:
            bot_id = message["bot_id"]
            logging.debug("bot addition for %s", bot_id)
            if "bot_link" in message:
                (bot_url, bot_name) = message["bot_link"].strip("<>").split("|", 1)
            elif "username" in message:
                bot_name = message["username"]
                bot_url = None
            else:
                bot_name = None
                bot_url = None

            self.__USER_DATA[bot_id] = User({
                "user": bot_id,
                "real_name": bot_name,
                "bot_url": bot_url,
                "is_bot": True,
                "is_app_user": True
            })

        user_id = message.get("user") or message.get("bot_id")
        if user_id not in self.__USER_DATA:
            if not message.get("user_profile"):
                logging.error("unable to find user in %s", message)
                return None

            new_user_data = {
                "id": user_id,
                "team_id": message["user_profile"].get("team"),
                "name": message["user_profile"].get("name"),
                "real_name": message["user_profile"].get("real_name"),
                "profile": {},
                "color": "db3150",
                "tz": "America/New_York",
                "tz_label": "Eastern Daylight Time",
                "tz_offset": -14400,
                "deleted": False,
                "is_admin": False,
                "is_owner": False,
                "is_primary_owner": False,
                "is_restricted": True,
                "is_ultra_restricted": True,
                "is_bot": False,
                "is_app_user": False,
                "updated": 1689891879,
                "is_email_confirmed": True,
            }

            biggest_image = 0
            for k,v in message["user_profile"].items():
                new_user_data["profile"].update({k: v})

                if k.startswith("image_"):
                    img_size = int(k.split("_")[1])
                    if img_size > biggest_image:
                        biggest_image = img_size

            if "image_512" not in new_user_data["profile"]:
                new_user_data["profile"]["image_512"] = new_user_data["profile"][f"image_{biggest_image}"]

            if "email" not in new_user_data["profile"]:
                new_user_data["email"] = "unknown"

            self.__USER_DATA.update({user_id: User(new_user_data)})

        return self.__USER_DATA.get(user_id)

    def replace_special_mentions(self, message):
        message = message.replace("<!channel>", "<a>@channel</a>")
        message = message.replace("<!channel|@channel>", "<a>@channel</a>")
        message = message.replace("<!here>", "<a>@here</a>")
        message = message.replace("<!here|@here>", "<a>@here</a>")
        message = message.replace("<!everyone>", "<a>@everyone</a>")
        message = message.replace("<!everyone|@everyone>", "<a>@everyone</a>")
        message = message.replace("<U00>", "<a><b>Someone</b></a>")
        return message

    def render_text(self, message, process_markdown=True):
        message = self.replace_special_mentions(message)

        # Handle mentions of users, channels and bots (e.g "<@U0BM1CGQY|calvinchanubc> has joined the channel")
        message = self._MENTION_PAT.sub(self._sub_annotated_mention, message)
        # Handle links
        message = self._LINK_PAT.sub(self._sub_hyperlink, message)
        # Handle hashtags (that are meant to be hashtags and not headings)
        message = self._HASHTAG_PAT.sub(self._sub_hashtag, message)

        # Introduce unicode emoji
        message = self.slack_to_accepted_emoji(message)
        message = emoji.emojize(message, language='alias')

        if process_markdown:
            # Handle bold (convert * * to ** **)
            message = re.sub(r'\*', "**", message)

            message = markdown2.markdown(
                message,
                extras=[
                    "cuddled-lists",
                    # This gives us <pre> and <code> tags for ```-fenced blocks
                    "fenced-code-blocks",
                    "pyshell"
                ]
            ).strip()

        # Special handling cases for lists
        message = message.replace("\n\n<ul>", "<ul>")
        message = message.replace("\n<li>", "<li>")

        return message

    def slack_to_accepted_emoji(self, message):
        """Convert some Slack emoji shortcodes to more universal versions"""
        # Convert -'s to _'s except for the 1st char (preserve things like :-1:)
        # For example, Slack's ":woman-shrugging:" is converted to ":woman_shrugging:"
        message = re.sub(
            r":([^ <>/:])([^ <>/:]+):",
            lambda x: ":{}{}:".format(x.group(1), x.group(2).replace("-", "_")),
            message
        )

        # https://github.com/Ranks/emojione/issues/114
        message = message.replace(":simple_smile:", ":slightly_smiling_face:")
        return message

    def _sub_annotated_mention(self, matchobj):
        ref_id = matchobj.group(1)[1:]  # drop #/@ from the start, we don't care
        annotation = matchobj.group(2)
        if ref_id.startswith('C'):
            mention_format = "<a><b>#{}</b></a>"
            if not annotation:
                channel = self.__CHANNEL_DATA.get(ref_id)
                annotation = channel["name"] if channel else ref_id
        else:
            mention_format = "<a>@{}</a>"
            if not annotation:
                user = self.__USER_DATA.get(ref_id)
                annotation = user.display_name if user else ref_id
        return mention_format.format(annotation)

    def _sub_hyperlink(self, matchobj):
        compound = matchobj.group(0)[1:-1]
        if len(compound.split("|")) == 2:
            url, title = compound.split("|")
        else:
            url, title = compound, compound
        result = "<a href=\"{url}\">{title}</a>".format(url=url, title=title)
        return result

    def _sub_hashtag(self, matchobj):
        text = matchobj.group(0)

        starting_space = " " if text[0] == " " else ""
        ending_space = " " if text[-1] == " " else ""

        return "{}<b>{}</b>{}".format(
            starting_space,
            text.strip(),
            ending_space
        )
