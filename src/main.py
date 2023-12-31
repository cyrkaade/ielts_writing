import logging
from dotenv import load_dotenv
import os
import openai
import redis
from typing import Dict
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, Audio, Voice
import speech_recognition as sr
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters
)

from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential
)

load_dotenv()

chatgpt_model = "gpt-3.5-turbo"


redis_host = os.getenv("REDIS_HOST")
redis_port = os.getenv("REDIS_PORT")
redis_db = os.getenv("REDIS_DB")
redis_password = os.getenv("REDIS_PASSWORD")

openai.api_key = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING, TYPING_REPLY = range(2)

reply_keyboard = [
    ["Writing-question", "Answer"],
    ["Assess"],
]
markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)

def redis_connection(redis_host, redis_port, redis_db, redis_password):
    try:
        redis_client = redis.Redis(host=redis_host, port=redis_port, db=redis_db, password=redis_password)
        return redis_client

    except redis.ConnectionError:
        print('Failed to connect to Redis')

def check_denied_user(user_id):
    redis_client = redis_connection(redis_host, redis_port, redis_db, redis_password)
    if redis_client is not None:
        if redis_client.exists(f"denied_{user_id}"):
            return True
        else:
            return False

def inputs_to_str(user_data: Dict[str, str]) -> str:
    inputs = [f"{key} - {value}" for key, value in user_data.items()]
    return "\n".join(inputs).join(["\n", "\n"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info("Sending /start from user %s", user.first_name)

    await update.message.reply_text(
        "Hi! I'm an IELTS examiner and want to help you to assess your writing skills. Please give me your IELTS Writing question and writing answer. \n"
        "Note that sometimes ChatGPT Servers have a high load, hence receiving your answer might take time, just wait for it.",
        reply_markup=markup
    )

    return CHOOSING


async def predefined_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["choice"] = text
    user = update.message.from_user
    logger.info("Sending choice %s from user %s", update.message.text, user.first_name)

    await update.message.reply_text(f"Please enter your {text.lower()}.")

    return TYPING_REPLY


async def received_information(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = context.user_data
    text = update.message.text
    category = user_data["choice"]
    user_data[category] = text
    del user_data["choice"]

    user = update.message.from_user
    logger.info("Sending this text from user %s text: %s", user.first_name, update.message.text)

    await update.message.reply_text(
        "Your response has been saved.",
        reply_markup=markup
    )

    return CHOOSING

@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
def chat_completion_with_backoff(**kwargs):
    return openai.ChatCompletion.create(**kwargs)

async def assess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = context.user_data
    user = update.message.from_user

    if "Writing-question" in user_data and "Answer" in user_data:
        if check_denied_user(user.id):
            await update.message.reply_text(
                "You can have only 1 assessment every 5 minute, try it after 5 minute again!",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.info("User %s is denied", user.id)

            user_data.clear()
            return ConversationHandler.END
        else:
            if "choice" in user_data:
                del user_data["choice"]

            completion = chat_completion_with_backoff(
                model=chatgpt_model,
                messages=[
                    {"role": "system", "content": "I want you to act as an IELTS writing examiner. I will give you examination questions and their answers, I need you to use assessment criteria to award a band score for each of the four criteria: Task Achievement (for Task 1), Task Response (for Task 2); Coherence and Cohesion; Lexical Resource; Grammatical Range and Accuracy. The four criteria scored a minimum of 1 point and a maximum of 9 points, all of which are multiples of 0.5, usually they are not the same. In addition, give me your model answer to the examination question. In Addition give an Overal band score."},
                    {"role": "user", "content": f'My writing topic is {user_data["Writing-question"]}'},
                    {"role": "user", "content": f'My writing answer is {user_data["Answer"]}'},
                ]
            )

            logger.info("Assesment: %s", completion.choices[0].message.content)

            message = completion.choices[0].message.content
            message_chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]

            for message_chunk in message_chunks:
                await update.message.reply_text(
                    message_chunk,
                    reply_markup=ReplyKeyboardRemove()
                )

            redis_client = redis_connection(redis_host, redis_port, redis_db, redis_password)
            if redis_client is not None:
                redis_client.set(f"denied_{user.id}", 1, ex=300)
                logger.info("Storing record to the redis for user: %s", user.id)

            user_data.clear()
            return ConversationHandler.END

    else:
        await update.message.reply_text(
            "You didn't enter one of Writing-question or Answer!"
        )


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                MessageHandler(
                    filters.Regex("^(Writing-question|Answer)$"), predefined_choice
                )
            ],
            TYPING_REPLY: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Assess$")),
                    received_information,
                )
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^Assess$"), assess)],
    )

    application.add_handler(conv_handler)
    application.run_polling()


if __name__ == "__main__":
    main()
