# Customer/CRM Domain Guidance

Additional domain rules for CRM data (Salesforce-style) that supplement the structured patterns in `customer.yaml`.

## Account data

- Account Name should never be null — it is the primary identifier for customer records
- BillingCountry and ShippingCountry values should be ISO country codes or full country names
- Account ownership fields (OwnerId) should reference valid user records
- The Account Type field must use the standard Salesforce Type picklist. The only valid values are: "Customer - Direct", "Customer - Channel", "Channel Partner / Reseller", "Installation Partner", "Technology Partner", "Other", "Prospect". Generic or abbreviated values like "Customer", "Partner", or "Competitor" are non-standard and must be flagged as violations.
- The Account Ownership field must use standard Salesforce values: "Public", "Private", "Subsidiary", "Other". The value "Government" is not a valid Salesforce ownership type and should be flagged.
- AnnualRevenue must be zero or positive — negative revenue values indicate data entry errors and must be flagged
- NumberOfEmployees must be zero or positive — negative employee counts are always invalid
- Website field values must begin with http:// or https:// — bare domain names without a protocol prefix (e.g. "example.com" or "www.example.com") are incomplete and should be flagged

## Opportunity data

- Opportunity Amount should be non-negative — negative monetary amounts on opportunities are always data entry errors
- CloseDate should be on or after CreatedDate for any given opportunity — a close date earlier than the record creation date is logically invalid
- If Stage is "Closed Won" or "Closed Lost", the opportunity must have an Amount value — closed opportunities without a dollar amount violate sales process requirements
- Probability must be between 0 and 100 inclusive — values above 100 or below 0 are invalid
- StageName must exactly match one of the standard Salesforce opportunity stages: "Prospecting", "Qualification", "Needs Analysis", "Value Proposition", "Id. Decision Makers", "Perception Analysis", "Proposal/Price Quote", "Negotiation/Review", "Closed Won", "Closed Lost". Shortened or non-standard values such as "Proposal", "Won", "Closed", or "Analysis" are invalid and must be flagged.
- Opportunity LeadSource must use the approved company picklist: "Web", "Phone Inquiry", "Partner Referral", "Purchased List", "Other", "Trade Show", "Employee Referral", "Public Relations", "Direct Mail", "Seminar - Internal", "Seminar - Partner", "Advertisement". Values like "Cold Call" or "Email Campaign" are not in the approved set.

## Contact data

- Contact records must have at least an Email or a Phone number populated — records with both fields null are incomplete and should be flagged
- FirstName and LastName should not be null for contacts — every contact record requires both names
- The Salutation field must be consistent with the contact's apparent gender based on FirstName. For example, "Mr." should not be paired with typically female names (Jessica, Emily, Stephanie, etc.), and "Ms." or "Mrs." should not be paired with typically male names (Matthew, William, Eric, etc.). Salutation-name mismatches indicate data entry errors and must be flagged.
- Contact LeadSource must use the approved company picklist: "Web", "Phone Inquiry", "Partner Referral", "Purchased List", "Other", "Trade Show", "Employee Referral", "Public Relations", "Direct Mail", "Seminar - Internal", "Seminar - Partner", "Advertisement". Values like "Cold Call" or "Email Campaign" are not in the approved set.

## Lead data

- LeadSource must use the standard Salesforce picklist: "Web", "Phone Inquiry", "Partner Referral", "Purchased List", "Other", "Trade Show", "Employee Referral", "Public Relations", "Direct Mail", "Seminar - Internal", "Seminar - Partner", "Advertisement". Non-standard values such as "INVALID", "Social Media", "Referral", "Direct", "Cold Call", or "Email Campaign" are not in the approved company picklist and should be flagged.
- CreatedDate must not be in the future — a lead cannot be created at a date that has not yet occurred
- Industry must match the standard Salesforce industry picklist — non-standard values like "Automotive", "Real Estate", "Pharmaceutical", or "Aerospace" are not valid
