"""Labelled REVIEW N1-N4 regressions for pure cache rules.

No models, server, application import, API call, or benchmark execution is needed.
"""
import unittest

from smart_ai_gateway.config import load_settings
from smart_ai_gateway.services.cache_checks import conflict, personal_phrase, time_sensitive


class LeaveCategoryRegressionTests(unittest.TestCase):
    def test_hard_negatives_have_explicit_different_leave_categories(self):
        pairs = [
            ("maternity versus paternity", "How do I apply for maternity leave?", "How do I apply for paternity leave?"),
            ("maternity spelling omission", "how do i aply for maternty leave", "how do I apply for paternity leave"),
            ("paternity spelling omission", "How do I request paternty leave?", "How do I request maternity leave?"),
            ("annual versus sick", "What is the process to request annual leave?", "What is the process to request sick leave?"),
            ("holiday versus medical", "How many holiday days am I entitled to?", "How many medical leave days am I entitled to?"),
            ("comparison versus one category", "Explain maternity and paternity leave", "Explain maternity leave"),
        ]
        for label, first, second in pairs:
            with self.subTest(label=label):
                self.assertIn("different leave categories", conflict(first, second))
                self.assertIn("different leave categories", conflict(second, first))

    def test_genuine_paraphrases_and_unrelated_annual_sick_words_are_not_vetoed(self):
        pairs = [
            ("maternity wording", "How do I apply for maternity leave?", "What is the procedure for maternity leave?"),
            ("maternity typo", "how do i aply for maternty leave", "how do I apply for maternity leave"),
            ("paternity typo", "how do i apply for paternty leave", "how do I apply for paternity leave"),
            ("annual and vacation synonyms", "How do I request annual leave?", "How do I request vacation days?"),
            ("sick and medical synonyms", "What documents do I need for sick leave?", "What paperwork is needed for medical leave?"),
            ("annual is not always leave", "How do I get the annual report?", "How do I get the report while sick?"),
            ("same categories in different order", "Explain maternity and paternity leave", "Explain paternity and maternity leave"),
        ]
        for label, first, second in pairs:
            with self.subTest(label=label):
                self.assertIsNone(conflict(first, second))


class TemporalRegressionTests(unittest.TestCase):
    def test_actual_current_or_upcoming_information_bypasses_cache(self):
        cases = [
            ("present status", "Is the VPN down at the moment?"),
            ("present alternative", "Is reception staffed at this moment?"),
            ("fire drill", "When is the next fire drill?"),
            ("another drill type", "Where is the next evacuation drill?"),
            ("company event", "When is the next company all-hands meeting?"),
            ("event without modifier", "When is the next all-hands?"),
            ("team event", "When is the next team meeting?"),
            ("holiday occurrence", "Is the office closed on the next public holiday?"),
            ("specific weekday", "Is the office open this Saturday?"),
            ("specific part of day", "What time does the shuttle leave this afternoon?"),
            ("named day", "Is the building open on Sunday?"),
            ("causal prefix does not hide current fact", "Now that I am back, who is the current manager?"),
            ("password procedure plus live status", "How do I change my current password while the VPN is down at the moment?"),
            ("carryover plus event", "Can I carry leave into next year, and when is the next safety drill?"),
            ("carryover plus separate deadline", "Can I carry leave into next year? What is the deadline this week?"),
            ("future policy change", "Will the rules for carrying leave into next year change?"),
            ("current credential status", "Is my current password compromised?"),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIsNotNone(time_sensitive(prompt), prompt)

    def test_stable_procedures_are_not_temporal_just_because_of_wording(self):
        cases = [
            ("causal now", "How do I turn off my automatic reply now that I have returned?"),
            ("credential adjective", "I remember my current password but want to change it to a new one. Where do I do that?"),
            ("credential pronoun", "How do I change my password if I know the current one?"),
            ("entering credentials", "Where should I enter my current password to reset it?"),
            ("leave carryover policy", "Can I carry over up to 5 unused annual leave days into next year?"),
            ("vacation carryover policy", "May unused vacation days roll over to next year?"),
            ("holiday carryover policy", "Do holiday days carry forward into next year?"),
            ("next step", "What is the next step for requesting leave?"),
            ("recurring day", "Is the building open every Sunday?"),
            ("recurring plural day", "Is reception open on Sundays?"),
            ("recurring event procedure", "How should I prepare for an annual fire drill?"),
            ("filler now", "Fat-fingered my password a few times and now I'm shut out, what now?"),
            ("first-person morning context", "I'm unwell this morning and can't make it to the office. Who should I notify?"),
            ("first-person weekday context", "I'm starting on Monday. What should I bring on my first day?"),
            ("first-person past event", "I left my company phone in a taxi last night. What are the urgent steps?"),
            ("urgency", "I clicked a link in a suspicious email. What should I do right now?"),
            ("first-person upcoming trip", "Can I get cash in advance for an upcoming business trip I'm taking?"),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIsNone(time_sensitive(prompt), prompt)

    def test_first_person_context_does_not_hide_shared_schedule_or_state(self):
        cases = [
            ("opening hours", "I'm off tomorrow, is the canteen open?"),
            ("room availability", "We have a client visit tomorrow, is the big room free?"),
            ("duty roster", "Who is on duty today? I need first aid"),
            ("menu", "My team wants to know what's for lunch today"),
            ("days remaining", "How many leave days do I have left as of today?"),
            ("next event", "I need to plan around it: when is the next fire drill?"),
            ("current state", "Now that I am back, who is the current manager?"),
            ("status phrase", "My VPN is down at the moment, is there an outage?"),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIsNotNone(time_sensitive(prompt), prompt)

    def test_first_person_fact_questions_about_a_date_still_bypass(self):
        # A first-person question that is not asking for a procedure depends on the date.
        cases = [
            ("work day", "Do we work tomorrow?"),
            ("pay timing", "Are we getting paid early this week?"),
            ("cover", "Who is covering for me today?"),
            ("permission today", "Can I leave early today?"),
            ("holiday", "Do we have a holiday next Monday?"),
            ("payday", "Is our payday this Friday?"),
            ("attendance", "Do I need to come in tomorrow?"),
            ("team event", "What are we doing for the team event tonight?"),
            ("request status", "Did my leave request from yesterday get approved?"),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIsNotNone(time_sensitive(prompt), prompt)

    def test_carry_over_balance_named_in_an_earlier_sentence(self):
        self.assertIsNone(time_sensitive(
            "It's nearly December and I will have 5 days of holiday unspent. "
            "Will I lose them or can they move into next year?"))
        self.assertIsNotNone(time_sensitive("Will the leave policy change next year? I have 5 days left."))


class PersonalPhraseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.phrases = load_settings(env={}).semantic_cache.personal_phrase_list

    def test_general_manager_procedure_questions_are_cache_eligible(self):
        cases = [
            ("request process", "How do I ask my manager for annual leave?"),
            ("permission policy", "Do I need my manager's permission to apply for an internal role?"),
            ("documentation process", "My manager mentioned paperwork. When do I need to provide a doctor's certificate for sick leave?"),
            ("preparing for review cycle", "My manager mentioned reviews are coming up soon. What is the timeline and what should I prepare?"),
            ("generic support", "Where can I find confidential counselling support without my boss knowing?"),
            ("approval procedure", "Does my manager need to approve a leave request?"),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIsNone(personal_phrase(prompt, self.phrases), prompt)

    def test_individual_decisions_names_and_sensitive_context_remain_private(self):
        cases = [
            ("individual rejection", "My manager rejected my claim. How do I appeal?"),
            ("named manager", "My manager Sarah rejected my leave request. What should I do?"),
            ("manager identification", "My manager is Sarah. How do I submit a claim to her?"),
            ("reported instruction", "My boss has told me to skip approval. What should I do?"),
            ("reported permission", "My manager said I can leave early, is that ok?"),
            ("present reported permission", "My manager says I can skip approval. What should I do?"),
            ("ongoing individual refusal", "My manager is refusing to approve my leave. What is the process to appeal?"),
            ("identity question", "What is my manager's name?"),
            ("sensitive grievance", "How do I report my manager for harassment?"),
            ("individual compensation", "My manager mentioned my salary. What form should I submit?"),
            ("repeated reference", "How do I ask my manager for leave? My manager already approved it privately."),
        ]
        for label, prompt in cases:
            with self.subTest(label=label):
                self.assertIn(personal_phrase(prompt, self.phrases), ("my manager", "my boss"), prompt)

    def test_manager_exception_does_not_disable_other_configured_phrases(self):
        prompt = "How do I ask my manager to correct what my payslip shows?"
        self.assertEqual(personal_phrase(prompt, self.phrases), "my payslip shows")
        self.assertEqual(personal_phrase("Is my manager said a configured phrase?", ["my manager said"]), "my manager said")
        self.assertEqual(personal_phrase("I'm new here, where do I apply?", ["I'm new here"]), "i'm new here")
        self.assertIsNone(personal_phrase("My manager rejected my claim", []))
        self.assertIsNone(personal_phrase("Is parking free for employees?", ["for employee"]))


if __name__ == "__main__":
    unittest.main()


class BenchmarkWrongMatchRegressionTests(unittest.TestCase):
    """Wrong matches found by the 19 Nov benchmark on the revised time-sensitive filter."""

    def test_acting_on_a_phishing_email_is_not_reporting_one(self):
        acted = [
            "I clicked a link in a suspicious email and entered my password. What should I do right now?",
            "Oh no I think I got phished, I typed my password into a dodgy page, what now?",
            "i clickd a bad link and put in my pasword help",
        ]
        report = [
            "I got a suspicious email asking for my login details. How do I report it?",
            "Kindly advise on the correct procedure to report a suspected phishing message.",
        ]
        for a in acted:
            for r in report:
                with self.subTest(acted=a, report=r):
                    self.assertIsNotNone(conflict(a, r))
                    self.assertIsNotNone(conflict(r, a))

    def test_negated_action_is_not_acting(self):
        # (The separate negation rule may still reject such a pair; this rule must not.)
        reason = conflict("I haven't clicked anything in the suspicious email, how do I report it?",
                          "I got a suspicious email asking for my login details. How do I report it?")
        self.assertNotEqual(reason, "acted on a suspicious message in only one prompt")

    def test_own_pay_is_not_a_supplier_invoice(self):
        self.assertIsNotNone(conflict("my pay is wrnog this month who do i ask",
                                      "Did you pay my invoice yet, who do I ask?"))
        self.assertIsNotNone(conflict("My salary this month looks wrong. Who do I contact?",
                                      "I'm a supplier and sent an invoice weeks ago. How do I find out if it's been paid?"))

    def test_same_question_wordings_still_match(self):
        for a, b in (("My salary this month looks wrong. Who do I contact to have it checked?", "my pay is wrnog this month who do i ask"),
                     ("clicked phishing link, entered password", "i clickd a bad link and put in my pasword help"),
                     ("Did you pay my invoice yet, who do I ask?", "how can i chek if my invioce was paid")):
            with self.subTest(a=a):
                self.assertIsNone(conflict(a, b))
