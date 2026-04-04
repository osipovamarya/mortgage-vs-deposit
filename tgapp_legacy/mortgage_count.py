from app.telegram_user import TelegramUser
# from app.mortgage import Mortgage
import collections
from dataclasses import dataclass
from datetime import datetime, timedelta
from dateutil import relativedelta
from dateutil.rrule import rrule, MONTHLY
import json
import logbook

from mortgage import Mortgage


class MortgageCount:
    def __init__(self, mortgage: Mortgage, chat_id: int, name: str = None):
        logbook.info(f'mortgage = {mortgage}')
        self.id = None
        self.chat_id = chat_id
        self.mortgage = mortgage
        self.name = name
        self.month_payment = mortgage.month_payment
        self.interest_sum_left = None
        logbook.info(f'MortgageCount = {self.__dict__}')

    @property
    def name(self):
        return self.__name

    @name.setter
    def name(self, name: str = None):
        self.__name = self.mortgage.name+'_count' if name is None else name


    def month_payment(self, date=None, repayment_sum=None):
        logbook.info(f'date = {date} and repayment_sum = {repayment_sum}')
        logbook.info(f'date = {type(date)} and repayment_sum = {type(repayment_sum)}')
        self.main_debt_sum -= float(repayment_sum)
        logbook.info(f'new_debt_sum {self.main_debt_sum}')
        super(MortgageCount, self).month_payment(date)
        # period_interest = float(self.interest) / (100 * 12)
        # payment = float(new_debt_sum) * (period_interest / (1 - (1 + period_interest) ** -self.__payment_period_num(date)))
        # self.__month_payment = round(payment, 2)


    # def new_month_payment(self, repayments: dict):
    #     period_interest = float(self.interest) / (100 * 12)
    #     repayment_sum = repayments['repayment_sum']
    #     logbook.info(f'repayment_sum {repayment_sum}')
    #     current_debt_sum = self.main_debt_sum - float('4762.46')
    #     logbook.info(f'current_debt_sum {current_debt_sum}')
    #     new_debt_sum = current_debt_sum - repayment_sum
    #     logbook.info(f'new_debt_sum {new_debt_sum}')
    #     payment = float(new_debt_sum) * (period_interest / (1 - (1 + period_interest) ** -self.__payment_period_num(repayments['date'])))
    #     return round(payment, 2)



    #
    # def start_estimation(self):
    #     self.phase = self.PHASE_ESTIMATION
    #
    # def end_estimation(self):
    #     self.phase = self.PHASE_RESOLUTION
    #
    # def clear_votes(self):
    #     self.estimation_votes.clear()
    #     self.phase = self.PHASE_ESTIMATION
    #
    # def re_estimate(self):
    #     self.estimation_votes.clear()
    #     self.phase = self.PHASE_ESTIMATION
    #
    # def add_discussion_vote(self, player, vote):
    #     self.discussion_votes[self.player_to_string(player)].set(vote)
    #
    # def add_estimation_vote(self, player, vote):
    #     self.estimation_votes[self.player_to_string(player)].set(vote)
    #
    # def render_system_message(self):
    #     return {
    #         "text": self.render_system_message_text(),
    #         "reply_markup": json.dumps(self.render_system_message_buttons()),
    #     }
    #
    # def render_system_message_text(self):
    #     result = ""
    #
    #     result += self.render_game_text()
    #     result += "\n"
    #     result += self.render_facilitator_text()
    #     result += "\n"
    #     result += self.render_topic_text()
    #     result += "\n"
    #     result += "\n"
    #     result += self.render_votes_text()
    #
    #     return result
    #
    # def render_game_text(self):
    #     if self.game is None:
    #         return ""
    #     else:
    #         return "Mortgage: {}".format(self.game.name)
    #
    # def render_facilitator_text(self):
    #     return "Facilitator: {}".format(self.facilitator.to_string())
    #
    # def render_topic_text(self):
    #     result = ""
    #
    #     if self.phase in self.PHASE_DISCUSSION:
    #         result += "Discussion for: "
    #     elif self.phase in self.PHASE_ESTIMATION:
    #         result += "Estimation for: "
    #     elif self.phase in self.PHASE_RESOLUTION:
    #         result += "Resolution for: "
    #
    #     result += self.topic
    #
    #     return result
    #
    # def render_votes_text(self):
    #     if self.phase in self.PHASE_DISCUSSION:
    #         return self.render_discussion_votes_text()
    #     elif self.phase in self.PHASE_ESTIMATION:
    #         return self.render_estimation_votes_text()
    #     elif self.phase in self.PHASE_RESOLUTION:
    #         return self.render_estimation_votes_text()
    #
    # def render_discussion_votes_text(self):
    #     result = ""
    #
    #     votes_count = len(self.discussion_votes)
    #
    #     if votes_count > 0:
    #         result += "Votes ({}):".format(votes_count)
    #         result += "\n"
    #         result += "\n".join(
    #             "{} {}".format(
    #                 discussion_vote.icon,
    #                 user_id,
    #             )
    #             for user_id, discussion_vote in sorted(self.discussion_votes.items())
    #         )
    #
    #     return result
    #
    # def render_estimation_votes_text(self):
    #     result = ""
    #
    #     votes_count = len(self.estimation_votes)
    #
    #     if votes_count > 0:
    #         result += "Votes ({}):".format(votes_count)
    #         result += "\n"
    #         result += "\n".join(
    #             "{} {}".format(
    #                 estimation_vote.vote if self.phase == self.PHASE_RESOLUTION else estimation_vote.masked,
    #                 user_id,
    #             )
    #             for user_id, estimation_vote in sorted(self.estimation_votes.items())
    #         )
    #
    #     return result
    #
    # def render_system_message_buttons(self):
    #     layout_rows = []
    #
    #     if self.phase in self.PHASE_DISCUSSION:
    #         layout_rows.append(
    #             [
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_TO_ESTIMATE,
    #                     "👍 To estimate",
    #                 ),
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_NEED_DISCUSS,
    #                     "⁉️ Discuss",
    #                 ),
    #             ]
    #         )
    #         layout_rows.append(
    #             [
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_SPLIT_TASK,
    #                     "✂️ Split",
    #                 ),
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_CANCEL_TASK,
    #                     "☠️️ Cancel",
    #                 ),
    #             ]
    #         )
    #         layout_rows.append(
    #             [
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_ESTIMATION_IMPOSSIBLE,
    #                     "♾️ Impossible",
    #                 ),
    #                 self.render_discussion_vote_button(
    #                     DiscussionVote.VOTE_TAKE_A_BREAK,
    #                     "☕️ Take a break",
    #                 ),
    #             ]
    #         )
    #         layout_rows.append(
    #             [
    #                 self.render_operation_button(self.OPERATION_START_ESTIMATION, "Start estimation"),
    #             ]
    #         )
    #     elif self.phase in self.PHASE_ESTIMATION:
    #         for votes_layout_row in self.CARD_DECK_LAYOUT:
    #             vote_buttons_row = []
    #             for vote in votes_layout_row:
    #                 vote_buttons_row.append(self.render_estimation_vote_button(vote))
    #             layout_rows.append(vote_buttons_row)
    #
    #         layout_rows.append(
    #             [
    #                 self.render_operation_button(self.OPERATION_CLEAR_VOTES, "Clear votes"),
    #                 self.render_operation_button(self.OPERATION_END_ESTIMATION, "End estimation"),
    #             ]
    #         )
    #     elif self.phase in self.PHASE_RESOLUTION:
    #         layout_rows.append(
    #             [
    #                 self.render_operation_button(self.OPERATION_RE_ESTIMATE, "Re-estimate"),
    #             ]
    #         )
    #
    #     return {
    #         "type": "InlineKeyboardMarkup",
    #         "inline_keyboard": layout_rows,
    #     }
    #
    # def render_discussion_vote_button(self, vote: str, text: str):
    #     return {
    #         "type": "InlineKeyboardButton",
    #         "text": text,
    #         "callback_data": "discussion-vote-click-{}-{}".format(self.facilitator_message_id, vote),
    #     }
    #
    # def render_estimation_vote_button(self, vote: str):
    #     return {
    #         "type": "InlineKeyboardButton",
    #         "text": vote,
    #         "callback_data": "estimation-vote-click-{}-{}".format(self.facilitator_message_id, vote),
    #     }
    #
    # def render_operation_button(self, operation: str, text: str):
    #     return {
    #         "type": "InlineKeyboardButton",
    #         "text": text,
    #         "callback_data": "{}-click-{}".format(operation, self.facilitator_message_id),
    #     }
    #
    # @staticmethod
    # def player_to_string(player: dict) -> str:
    #     return "@{} ({})".format(
    #         player.get("username") or player.get("id"),
    #         "{} {}".format(player.get("first_name"), player.get("last_name") or "").strip()
    #     )
    #
    # @staticmethod
    # def votes_to_json(votes):
    #     return {
    #         user_id: vote.to_dict() for user_id, vote in votes.items()
    #     }
    #
    # def to_dict(self):
    #     return {
    #         "facilitator": self.facilitator.to_dict(),
    #         "discussion_votes": self.votes_to_json(self.discussion_votes),
    #         "estimation_votes": self.votes_to_json(self.estimation_votes),
    #     }
    #
    # @classmethod
    # def from_dict(cls, game: Mortgage, chat_id: int, facilitator_message_id: int, topic: str, facilitator: TelegramUser, dict):
    #     result = cls(
    #         game,
    #         chat_id,
    #         facilitator_message_id,
    #         topic,
    #         facilitator,
    #     )
    #
    #     for user_id, discussion_vote in dict["discussion_votes"].items():
    #         result.discussion_votes[user_id] = DiscussionVote.from_dict(discussion_vote)
    #
    #     for user_id, estimation_vote in dict["estimation_votes"].items():
    #         result.estimation_votes[user_id] = EstimationVote.from_dict(estimation_vote)
    #
    #     return result
