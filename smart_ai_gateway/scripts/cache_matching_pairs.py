"""
Labelled question pairs for evaluate_cache_matching.py: (cached question, new question, same?).

"Same" means one cached answer correctly answers both questions. The "different" pairs are
deliberately hard: near-identical wording where one detail changes the answer.
"""

SAME = [
    ("how do I reset my password", "I forgot my password, how can I change it"),
    ("how do I connect to the VPN from home", "what are the steps to use the VPN when working remotely"),
    ("how many days of annual leave do I get", "what is my yearly vacation entitlement"),
    ("where can I find my payslip", "how do I view my salary statement"),
    ("how do I book a meeting room", "what's the process for reserving a conference room"),
    ("my laptop is running very slowly", "my computer has become really slow"),
    ("how do I set up email on my phone", "how can I get my work email on my mobile"),
    ("who do I contact for IT support", "how do I reach the IT helpdesk"),
    ("how do I submit an expense claim", "what is the process to claim back expenses"),
    ("can I work from home on fridays", "is remote work allowed on fridays"),
    ("how do I add a printer to my laptop", "how can I install a printer on my computer"),
    ("what is the wifi password for guests", "what's the guest wifi password"),
    ("how do I request a new monitor", "how can I order an additional screen for my desk"),
    ("how do I change my desk phone voicemail greeting", "how do I record a new voicemail message on my office phone"),
    ("what time does the office open", "what are the office opening hours"),
    ("how do I share a file with an external partner", "how can I send a document to someone outside the company"),
    ("my Outlook keeps crashing", "Outlook crashes all the time"),
    ("how do I enable two factor authentication", "how do I turn on 2FA"),
    ("how do I update my home address in the HR system", "where do I change my address on the HR portal"),
    ("the printer on floor 3 is jammed", "there is a paper jam in the floor 3 printer"),
    ("how do I get access to the shared drive", "how can I request permission for the shared folder"),
    ("how long is maternity leave", "how many weeks of maternity leave are we entitled to"),
    ("how do I clear my browser cache", "what are the steps to delete cached files in my web browser"),
    ("I can't log in to my account", "I'm unable to sign in to my account"),
    ("how do I install Zoom", "how can I download and set up Zoom on my computer"),
    ("what is the dress code", "is there a dress policy for the office"),
    ("how do I report a phishing email", "I received a suspicious email, who do I tell"),
    ("when is payday", "what day do we get paid each month"),
    ("how do I unlock my account", "my account is locked, how do I get back in"),
    ("how do I reset my password", "password reset steps"),
    ("what is the company holiday policy", "what's our policy on public holidays"),
    ("how do I connect to the office wifi", "how can I join the wireless network at the office"),
]

DIFFERENT = [
    ("how do I reset my Outlook password", "how do I reset my Gmail password"),
    ("how do I enable two factor authentication", "how do I disable two factor authentication"),
    ("how do I fix VPN error 809", "how do I fix VPN error 720"),
    ("how do I install Teams", "how do I uninstall Teams"),
    ("how many days of sick leave do I get", "how many days of annual leave do I get"),
    ("how do I connect to the VPN on Windows", "how do I connect to the VPN on a Mac"),
    ("can I bring my dog to the office", "can I bring my child to the office"),
    ("how do I add a user to the Teams channel", "how do I remove a user from the Teams channel"),
    ("what is the wifi password for guests", "what is the wifi password for staff"),
    ("my laptop won't turn on", "my laptop won't turn off"),
    ("how do I book a meeting room", "how do I cancel a meeting room booking"),
    ("where is the office in London", "where is the office in Singapore"),
    ("who approves my expense claims", "who approves my leave requests"),
    ("how do I upgrade to Windows 11", "how do I upgrade to Windows 10"),
    ("how do I log in to the HR portal", "how do I log out of the HR portal"),
    ("is parking free for employees", "is parking free for visitors"),
    ("how do I request a new laptop", "how do I return my old laptop"),
    ("what is the deadline for submitting timesheets", "what is the deadline for submitting expense claims"),
    ("how do I share my screen in Zoom", "how do I share my screen in Teams"),
    ("I can access the shared drive", "I can't access the shared drive"),
    ("how do I export my contacts from Outlook", "how do I import my contacts into Outlook"),
    ("what happens if I work overtime on a weekday", "what happens if I work overtime on a public holiday"),
    ("how do I set an out of office reply", "how do I turn off my out of office reply"),
    ("how do I get reimbursed for travel", "how do I book travel for a business trip"),
    ("how do I reset my password", "how do I reset my PIN"),
    ("how do I bake sourdough bread", "how do I reset my password"),
]

PAIRS = [(a, b, True) for a, b in SAME] + [(a, b, False) for a, b in DIFFERENT]
