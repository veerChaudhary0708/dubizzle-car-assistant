# Submission demo: real Gemini, isolated data

Recorded: 2026-11-12T14:19:26.247589+00:00. Model: `gemini-3.5-flash`.

Reproduce with `uv run python scripts/demo.py` after configuring your own API key. This consumes API quota. The script uses the real FastAPI routes and Gemini with a disposable database; it never opens the running application's saved chats.

The replies below are actual model outputs, not expected-response templates. Session labels are synthetic. Source hashes and machine-checked assertions are in [submission-demo.json](submission-demo.json).

## 1. Multi-turn inventory exploration

**New session `memory-1`; shopper `submission-memory-shopper`; saved turns before the first message: 0.**

**Shopper:** My budget is AED 25,000. I prefer convertibles. Find a Ford Mustang under AED 25,000.

**Assistant:**

I found 1 Ford Mustang convertible under AED 25,000:

1. **2014 Ford Mustang V6 Convertible** (Listing ID: 20)
   * **Price:** AED 21,500
   * **Mileage:** 164,000 km
   * **Details:** 3.7L V6 engine, silver exterior with fabric interior, soft top convertible, cruise control, Bluetooth, climate control, tiptronic gears, and rear parking sensors. *(Note: 300 BHP power output is a seller description claim).*

Would you like to schedule a viewing or test drive for this Mustang, or inspect more details about it?

**Shopper:** What's its mileage?

**Assistant:**

The 2014 Ford Mustang V6 Convertible has an odometer reading of **164,000 km**. 

*(Note: Standardized fuel economy measurements are not provided in the vehicle specifications).*

Would you like to schedule a viewing or test drive for this Mustang, or inspect any other details?

**Shopper:** Does it have a warranty?

**Assistant:**

No warranty information is specified in the vehicle's details or seller description for the 2014 Ford Mustang V6 Convertible. 

A pre-purchase technical inspection would be recommended to evaluate its current mechanical condition and resolve any uncertainty. 

Would you like to schedule a viewing or test drive to inspect the vehicle in person?

**Shopper:** What does V6 mean?

**Assistant:**

A **V6 engine** refers to an engine layout with six cylinders arranged in two opposing banks of three in a "V" shape around a shared crankshaft. 

In general automotive design:
* **Configuration:** V6 engines offer a balance between compact size and smooth power delivery, generally producing more power than a typical 4-cylinder engine while being lighter and more compact than a V8.
* **In this Mustang:** It refers to the 3.7-liter 6-cylinder powertrain *(the seller description notes a output of 300 BHP)*.

Engine layout alone does not determine fuel economy, long-term reliability, or maintenance costs, which depend on specific configuration, driving habits, and maintenance history. 

Would you like to schedule a viewing or test drive for this Mustang, or check any other details?

**Shopper:** I like this car.

**Assistant:**

I've saved the 2014 Ford Mustang V6 Convertible (Listing ID: 20) to your saved cars!

Would you like to schedule a viewing or a test drive for it, or explore anything else?

## 2. Returning shopper in a completely new session

**New session `memory-2`; shopper `submission-memory-shopper`; saved turns before the first message: 0.**

**Shopper:** What was my budget, and which car did I like last time?

**Assistant:**

In your last visit, your maximum budget was **AED 25,000**. 

The car you liked was a **2014 Ford Mustang V6 convertible** (Listing #20), priced at **AED 21,500** with 164,000 km.

## 3. Gather price range and needs; export a lead without booking

**New session `lead-1`; shopper `submission-lead-shopper`; saved turns before the first message: 0.**

**Shopper:** What cars are available?

**Assistant:**

We have a wide selection of vehicles available in inventory across various makes and body styles, including SUVs, sedans, coupes, and luxury models. 

Here are the first few options:
1. **2013 Ford Explorer** (Silver)
2. **2019 Mercedes-Benz C 300** (c300 luxury)
3. **2018 Land Rover Range Rover Velar** — AED 119,750 (68,000 km)
4. **2019 Mercedes-Benz E 450 Coupe** (56,000 km)
5. **2017 Mercedes-Benz C 63 Coupe AMG** (48,000 km)

What price range in AED works for you?

**Shopper:** Between AED 20,000 and AED 30,000

**Assistant:**

Under your budget of **AED 20,000 to AED 30,000**, here is the verified option currently available:

1. **2014 Ford Mustang Soft Top Convertible V6** (Listing 20)
   - **Price:** AED 21,500
   - **Mileage:** 164,000 km
   - **Features:** Silver with fabric interior, 3.7L V6 (300 hp), soft top convertible, Bluetooth, cruise control, climate control, and rear parking sensors.

*(Note: There are 87 other listings in our system with unlisted prices that cannot be verified under this budget.)*

What kind of car or specific features do you need for your search?

**Shopper:** Automatic with room for two child seats

**Assistant:**

I have updated your preferences to record that you need an **automatic vehicle with room for two child seats**.

Under your current budget of **AED 20,000 to AED 30,000**:
- The only verified match in inventory is the **2014 Ford Mustang Soft Top Convertible V6** (AED 21,500). However, as a two-door sports convertible, its rear seating space may be quite tight or impractical for accommodating two child seats comfortably.
- There are no other verified listings under AED 30,000 at this moment. We do have unlisted-price family models in inventory (such as the Ford Explorer, Mitsubishi Pajero, and Lincoln MKX), but their prices are not verified within this range.

Would you like to consider expanding your budget limit or looking into specific family-friendly body styles (like 4-door sedans or SUVs)?

**Actual exported lead CSV:**

```csv
user_id,session_id,budget_min_aed,budget_max_aed,needs,updated_at
submission-lead-shopper,lead-1,20000,30000,"Automatic with room for two child seats, automatic",2026-11-12T14:20:50+00:00
```

## 4. Review and confirm a simulated booking

**Shopper:** Book a test drive of the 2014 Ford Mustang on 2026-11-16 at 10 AM Dubai time.

**Assistant:**

I have prepared a proposal for your test drive:

* **Vehicle:** 2014 Ford Mustang V6 Convertible (Listing ID: 20)
* **Type:** Test Drive
* **Date:** Monday, November 16, 2026
* **Time:** 10:00 AM (Dubai time)

This proposal is currently **awaiting your confirmation** and no appointment is reserved yet. Please confirm if you would like to proceed with this booking (you can reply "yes" or click "Confirm booking").

Backend proposal: listing 20, test_drive, `2026-11-16T10:00:00+04:00`, status `awaiting_confirmation`. No appointment has been reserved.

**Shopper:** Before I confirm, remind me of its mileage.

**Assistant:**

The 2014 Ford Mustang V6 Convertible has an odometer reading of **164,000 km**.

Your test drive proposal details remain saved as:
* **Vehicle:** 2014 Ford Mustang V6 Convertible (Listing ID: 20)
* **Type:** Test Drive
* **Date:** Monday, November 16, 2026
* **Time:** 10:00 AM (Dubai time)

This proposal is still **awaiting your final confirmation**. Would you like to go ahead and confirm this test drive now?

**Client action:** Confirm the exact current proposal through `POST /sessions/{session_id}/booking-confirmation`.

**Backend:** Your simulated test drive for the 2014 Ford Mustang is confirmed for 2026-11-16 at 10:00 Dubai time (UTC+04:00). This is a demo booking; no dealership has been contacted.

```json
{
  "booking_id": "booking-12",
  "status": "confirmed",
  "listing_id": 20,
  "starts_at": "2026-11-16T10:00:00+04:00",
  "kind": "test_drive",
  "car_label": "2014 Ford Mustang V6",
  "session_id": "e876355f26674ccea0ffc4318f72e639"
}
```

## Verification

Automated result: **passed**; 43/43 checks passed.

Provider calls: 22. Confirmation and its retry were checked with provider generation disabled.

- PASS: Backend imports exactly 100 inventory listings
- PASS: memory-1: session created
- PASS: memory-1: starts with no saved messages
- PASS: memory-1: My budget is AED 25,000. I prefer convertibles. Find a Ford Mustang under AED 25,000. — HTTP 200
- PASS: Mustang search returns listing 20
- PASS: Mustang asking price is AED 21,500
- PASS: memory-1: What's its mileage? — HTTP 200
- PASS: Mileage answer contains the verified 164,000 km
- PASS: memory-1: Does it have a warranty? — HTTP 200
- PASS: memory-1: What does V6 mean? — HTTP 200
- PASS: memory-1: I like this car. — HTTP 200
- PASS: Explicit personal budget was persisted
- PASS: First conversation has not booked anything
- PASS: memory-2: session created
- PASS: memory-2: starts with no saved messages
- PASS: New session has no selected car
- PASS: New session retains the same shopper's budget
- PASS: memory-2: What was my budget, and which car did I like last time? — HTTP 200
- PASS: Fresh-session reply recalls AED 25,000
- PASS: Fresh-session reply recalls the Mustang
- PASS: lead-1: session created
- PASS: lead-1: starts with no saved messages
- PASS: Different shopper does not inherit preferences
- PASS: lead-1: What cars are available? — HTTP 200
- PASS: Browsing asks a budget question
- PASS: lead-1: Between AED 20,000 and AED 30,000 — HTTP 200
- PASS: Budget reply asks about vehicle requirements
- PASS: lead-1: Automatic with room for two child seats — HTTP 200
- PASS: CSV contains the stated AED 20,000–30,000 range
- PASS: CSV captures automatic transmission and child-seat needs
- PASS: Lead collection does not require an appointment
- PASS: memory-1: Book a test drive of the 2014 Ford Mustang on 2026-11-16 at 10 AM Dubai time. — HTTP 200
- PASS: Complete request produces an awaiting-confirmation proposal
- PASS: Proposal contains exact car, type and time
- PASS: Preparing the proposal reserves nothing
- PASS: memory-1: Before I confirm, remind me of its mileage. — HTTP 200
- PASS: Mileage question preserves the exact proposal
- PASS: Question before confirmation reserves nothing
- PASS: Explicit confirmation succeeds without Gemini
- PASS: Receipt has the exact reviewed car, type and time
- PASS: Retry returns the same receipt
- PASS: Exactly one appointment exists
- PASS: Other shopper cannot see the appointment

Model prose still requires review: confirm that the warranty answer does not invent coverage, V6 explanation distinguishes general knowledge from listing facts, the fresh-session answer recalls the correct shopper, and the lead questions are relevant. Passing one recorded run is not a guarantee of all future model wording.

## Review of this recorded run

The captured replies and backend assertions were reviewed after the run. All four scenarios satisfy their stated checks.

- Search and follow-up mileage identify listing 20, AED 21,500 and 164,000 km.
- Warranty is explicitly described as unspecified in both structured details and seller description; no coverage is invented.
- V6 is explained as six cylinders arranged in a V; the 300 BHP figure is labelled as a seller description claim in the explanation.
- A completely new session starts with zero turns and no selected car, then recalls the same shopper budget and liked Mustang.
- The separate shopper is asked for budget and then needs. A literal short needs answer is accepted and the actual CSV contains both bounds and automatic/child-seat needs.
- The model distinguishes unknown-priced family alternatives from verified budget matches and qualifies the Mustang child-seat suitability as general practical advice.
- Booking prose describes a proposal awaiting confirmation, including after a mileage question. The receipt appears only after the exact proposal API confirmation.
- Confirmation and retry are independent of Gemini; the actual booking API returns exactly one appointment and none for the other shopper.

Wording limitations: several replies repeat a viewing/test-drive invitation, and the mileage answer adds an unnecessary fuel-economy note. These do not alter the recorded facts or bookings, but the model is not being claimed universally reliable.
