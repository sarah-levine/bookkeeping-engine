"""
Payroll client modules. Each module handles parsing and journal entry
generation for a specific ADP statement format. Adding a new client = new
module here (or reuse an existing format runner with a different config).
"""
from payroll_clients.adp_payroll_professional import run_adp_payroll_professional
from payroll_clients.adp_payroll_1099        import run_adp_payroll_1099
from payroll_clients.adp_payroll_details     import run_adp_payroll_details
from payroll_clients.adp_payroll_tipped      import run_adp_payroll_tipped
from payroll_clients.adp_payroll_departments import run_adp_payroll_departments
from payroll_clients.adp_labor_distribution  import run_adp_labor_distribution
