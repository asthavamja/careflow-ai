from agents.scheduling import find_offers
from agents.trail import change_status, create_approval, save_offers
from api.db import get_connection


class BookingError(Exception):
    def __init__(self, code, message, new_offers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.new_offers = new_offers or []


def book_choice(request_id: int, choice: int) -> dict:
    taken_department = None
    with get_connection() as conn:
        req = conn.execute(
            """select id, status, patient_id, offered_slots
               from careflow.patient_requests where id = %s for update""",
            (request_id,),
        ).fetchone()
        if req is None:
            raise BookingError("not_found", "Request not found")
        offers = req["offered_slots"] or []
        if req["status"] != "waiting_patient" or not offers:
            raise BookingError("not_waiting", "This request is not waiting for a slot choice")
        if not req["patient_id"]:
            raise BookingError("no_patient", "No patient record is linked to this request yet")
        if choice < 1 or choice > len(offers):
            raise BookingError("invalid_choice",
                               f"Please choose one of the offered options, 1 to {len(offers)}")
        slot = offers[choice - 1]

        # One single step: book the slot only if it is still free
        got = conn.execute(
            """update careflow.time_slots set status = 'booked', held_until = null
               where id = %s and start_time > now()
                 and (status = 'free' or (status = 'held' and held_until < now()))
               returning id""",
            (slot["slot_id"],),
        ).fetchone()

        if got is not None:
            appt = conn.execute(
                """insert into careflow.appointments (patient_id, slot_id, request_id)
                   values (%s, %s, %s) returning id, appointment_code""",
                (req["patient_id"], slot["slot_id"], request_id),
            ).fetchone()
            conn.execute(
                "update careflow.patient_requests set status = 'executing', updated_at = now() where id = %s",
                (request_id,),
            )
            conn.execute(
                """insert into careflow.request_events
                     (request_id, from_status, to_status, actor, note)
                   values (%s, 'waiting_patient', 'executing', 'system', %s)""",
                (request_id, f"Patient chose option {choice}; appointment booked"),
            )
            return {
                "status": "booked",
                "message": "Your appointment is booked.",
                "appointment_id": appt["id"],
                "appointment_code": appt["appointment_code"],
                "appointment": slot["label"],
            }

        taken_department = conn.execute(
            """select d.department_id from careflow.time_slots s
               join careflow.doctors d on d.id = s.doctor_id where s.id = %s""",
            (slot["slot_id"],),
        ).fetchone()["department_id"]

    # The slot was taken: offer fresh ones (outside the transaction above)
    fresh = find_offers(taken_department, None)["offers"]
    if not fresh:
        create_approval(request_id, "other")
        change_status(request_id, "awaiting_approval", "system",
                      "Chosen slot was taken and no other slots are free")
        raise BookingError("no_slots", "That slot is no longer available, and no other slots are free. Our staff will contact you.")
    save_offers(request_id, fresh)
    raise BookingError("slot_unavailable",
                       "That slot is no longer available. Please choose one of these instead.", fresh)