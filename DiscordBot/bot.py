# bot.py
import discord
from discord.ext import commands
from unidecode import unidecode # for disguised unicode characters
from simpletransformers.classification import ClassificationModel # for loading Simple Transformer classification model
import os
import json
import logging
import re
import requests
import random
from report import Report

N_THRESHOLD = 2
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Set up logging to the console
logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

# There should be a file called 'token.json' inside the same folder as this file
token_path = 'tokens.json'
if not os.path.isfile(token_path):
    raise Exception(f"{token_path} not found!")
with open(token_path) as f:
    # If you get an error here, it means your token is formatted incorrectly. Did you put it in quotes?
    tokens = json.load(f)
    discord_token = tokens['discord']
    perspective_key = tokens['perspective']


class ModBot(discord.Client):
    def __init__(self, key):
        intents = discord.Intents.default()
        super().__init__(command_prefix='.', intents=intents)
        self.group_num = None
        self.mod_channels = {} # Map from guild to the mod channel id for that guild
        self.reports = {} # Map from user IDs to the state of their report
        self.perspective_key = key
        self.currReportAuthor = None
        self.currReportID = None
        self.currReport = None
        self.karma = {}  # Map from user IDs to the number of times they've been reported
        self.queue = [] # List for the queue of user reports waiting for moderator response
        # Loading our saved Simple Transformer classifier model
        self.model = ClassificationModel(model_type='roberta', model_name='checkpoint-3750-epoch-10', use_cuda=False) 

    async def on_ready(self):
        print(f'{self.user.name} has connected to Discord! It is these guilds:')
        for guild in self.guilds:
            print(f' - {guild.name}')
        print('Press Ctrl-C to quit.')

        # Parse the group number out of the bot's name
        match = re.search('[gG]roup (\d+) [bB]ot', self.user.name)
        if match:
            self.group_num = match.group(1)
        else:
            raise Exception("Group number not found in bot's name. Name format should be \"Group # Bot\".")

        # Find the mod channel in each guild that this bot should report to
        for guild in self.guilds:
            for channel in guild.text_channels:
                if channel.name == f'group-{self.group_num}-mod':
                    self.mod_channels[guild.id] = channel

    async def on_message(self, message):
        '''
        This function is called whenever a message is sent in a channel that the bot can see (including DMs). 
        Currently the bot is configured to only handle messages that are sent over DMs or in your group's "group-#" channel. 
        '''
        # Ignore messages from the bot 
        if message.author.id == self.user.id:
            return

        # Check if this message was sent in a server ("guild") or if it's a DM
        if message.guild:
            await self.handle_channel_message(message)
        else:
            await self.handle_dm(message)

    async def on_message_edit(self, before, after):
        await self.handle_channel_edit(after)

    async def handle_dm(self, message):
        # Handle a help message
        if message.content == Report.HELP_KEYWORD:
            reply =  "Use the `report` command to begin the reporting process.\n"
            reply += "Use the `cancel` command to cancel the report process.\n"
            await message.channel.send(reply)
            return

        author_id = str(message.author.id)
        responses = []

        # Only respond to messages if they're part of a reporting flow
        if author_id not in self.reports and not message.content.startswith(Report.START_KEYWORD):
            return

        # If we don't currently have an active report for this user, add one
        if author_id not in self.reports:
            self.queue.append(author_id)
            self.reports[author_id] = Report(self)
            self.reports[author_id].reporter = message.author.name

        # Let the report class handle this message; forward all the messages it returns to us
        responses = await self.reports[author_id].handle_message(message)
        for r in responses:
            await message.channel.send(r)
        
        reported_id = self.reports[author_id].reportedMessage.author.id
        # If no track record of author_id, add one
        if reported_id not in self.karma:
            self.karma[reported_id] = 0

        # If the report is complete or cancelled, remove it from our map
        if self.reports[author_id].report_complete():
            # If the report was properly completed, finish flow on moderator's side
            if not message.content == 'cancel':
                self.karma[reported_id] += 1          # increment author_id record by one
                # Only start the moderator's reporting flow if this report is at the front of the queue
                if len(self.queue) == 1:
                    await self.start_mod_flow()
            else:
                self.queue.pop(0)
                self.reports.pop(author_id)
    
    # changing this from a regular def function to an async function
    async def start_mod_flow(self): 
        if len(self.queue) == 0:
            return
        # Sending the report message for the first item is the self.queue list   
        self.currReportID = self.queue[0]
        self.currReport = self.reports[self.currReportID]
        self.currReportAuthor = self.currReport.reporter
        reported_m = self.currReport.reportedMessage         
        mod_channel = self.mod_channels[reported_m.guild.id]

        if self.currReportAuthor == 'auto':
            reply = "NEW REPORT \nmade by `COVID-19 misinformation Bot " + "` regarding a post by `" + reported_m.author.name + "`"
            reply += "\n\n And here is the message content: ```" + reported_m.content + "```"
            reply += "\nIs a response necessary? Please enter `yes` or `no`."
            await mod_channel.send(reply)
        else:
            # Foward the complete report to the mod channel
            reply = "NEW REPORT \nmade by `" + self.currReportAuthor + "` regarding a post by `" + reported_m.author.name + "`"
            reply += "\n• The message reported falls under **" + self.currReport.broadCategory + "**"
            reply += "\n• And is more specifically related to **" + self.currReport.specificCategory + "**"
            reply += "\n• Here is an optional message from the reporter: **" + self.currReport.optionalMessage + "**"
            reply += "\n• Would the reporter like to no longer see posts from the same user? **" + self.currReport.postVisibility + "**"       
            if self.currReport.postVisibility == 'yes':
                reply += "\nHow would the reporter like to change the status of the offending user's relationship with them? **" + self.currReport.userVisibility + "**"
            reply += "\n\n And here is the message content: ```" + reported_m.content + "```"
            if self.karma[reported_m.author.id] >= N_THRESHOLD:
                reply += "ATTENTION: This user has been reported " + str(N_THRESHOLD) + " times. It may be appropriate to take further action by restricting this user."
            if self.currReport.broadCategory == 'Misinformation':
                reply += "\nIs a response necessary? Please enter `yes`, `no`, or `unclear`."
            else:
                reply += "\nIs a response necessary? Please enter `yes` or `no`."
            await mod_channel.send(reply)

    async def handle_mod_message(self, message): 
        mod_channel = self.mod_channels[message.guild.id]
        author_id = self.currReportID
        if message.content == 'yes':
            # Post needs to be removed
            await self.currReport.reportedMessage.add_reaction('❌') 
            await mod_channel.send("This post has been deleted. This post removal is symbolized by the ❌ reaction on it.")
        elif author_id != "auto" and self.currReport.broadCategory == 'Misinformation':
            # If not misinfo but high risk, add warning and de prioritize
            if message.content == 'no':
                await self.handle_special_cases() 
                await mod_channel.send("This post has been de-prioritized and given a warning label. These actions are symbolized by the 🔻 and ⭕ reactions respectively")
            if message.content == 'unclear':
                # send to fact checker function
                if random.randrange(100) < 50:
                    await self.currReport.reportedMessage.add_reaction('❌') 
                    await mod_channel.send("This post has been classified as false by the fact checker so it had been deleted. This post removal is symbolized by the ❌ reaction on it.")
                else:
                    await self.handle_special_cases() 
                    await mod_channel.send("This post has been classified as true by the fact checker so it has only been de-prioritized and given a warning label. These actions are symbolized by the 🔻 and ⭕ reactions respectively.")
        # deal with karma here - if karma bad, suspend user
        # Start next report if it exists
        self.queue.pop(0)
        self.reports.pop(author_id)
        await self.start_mod_flow()
        return
    
    async def handle_special_cases(self):
        # Check if it is high risk
        if self.currReport.specificCategory in {'Elections', 'Covid-19', 'Other Health or Medical'}:
            await self.currReport.reportedMessage.add_reaction('🔻') # this emoji represents de-prioritization (ie shown to less people) 
            await self.currReport.reportedMessage.add_reaction('⭕') # this emoji represents a warning label
        return

    async def handle_channel_message(self, message):
        # Send the info to mod function if necessary
        if message.channel.name == f'group-{self.group_num}-mod':
            await self.handle_mod_message(message)
            return

        # Only handle messages sent in the "group-#" channel
        if not message.channel.name == f'group-{self.group_num}':
            return

        # Forward the message to the mod channel
        mod_channel = self.mod_channels[message.guild.id]
        await mod_channel.send(f'Forwarded message:\n{message.author.name}: "{message.content}"')

        # Use the classifier to determine if the message contains misinformation
        predictions, raw_output = self.model.predict([message.content])
        if predictions[0] == 0 or predictions[0] == 2:
            # If content is COVID misinformation, automatic flag and generate report to mod channel
            report = Report(self)
            report.reportedMessage = message
            report.reporter = 'auto'
            self.reports['auto'] = report
            self.queue.append('auto')
            if len(self.queue) == 1:
                await self.start_mod_flow()

    async def handle_channel_edit(self, message):
        # Send the info to mod function if necessary
        if message.channel.name == f'group-{self.group_num}-mod':
            await self.handle_mod_message(message)

        # Only handle messages sent in the "group-#" channel
        if not message.channel.name == f'group-{self.group_num}':
            return

        # Forward the message to the mod channel
        mod_channel = self.mod_channels[message.guild.id]
        await mod_channel.send(f'ALERT: message has been edited! Forwarded message:\n{message.author.name}: "{message.content}"')

        scores = self.eval_text(message)
        await mod_channel.send(self.code_format(json.dumps(scores, indent=2)))

    def eval_text(self, message):
        '''
        Given a message, forwards the message to Perspective and returns a dictionary of scores.
        '''
        
        # Transliterate unicode string into closest possible representation in ASCII text
        message.content = unidecode(message.content)

        PERSPECTIVE_URL = 'https://commentanalyzer.googleapis.com/v1alpha1/comments:analyze'

        url = PERSPECTIVE_URL + '?key=' + self.perspective_key
        data_dict = {
            'comment': {'text': message.content},
            'languages': ['en'],
            'requestedAttributes': {
                                    'SEVERE_TOXICITY': {}, 'PROFANITY': {},
                                    'IDENTITY_ATTACK': {}, 'THREAT': {},
                                    'TOXICITY': {}, 'FLIRTATION': {}
                                },
            'doNotStore': True
        }
        response = requests.post(url, data=json.dumps(data_dict))
        response_dict = response.json()

        scores = {}
        for attr in response_dict["attributeScores"]:
            scores[attr] = response_dict["attributeScores"][attr]["summaryScore"]["value"]

        return scores

    def code_format(self, text):
        return "```" + text + "```"


client = ModBot(perspective_key)
client.run(discord_token)
