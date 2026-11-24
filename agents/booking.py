import logging

from agents.scheduling import find_offers, find_reschedule_offers
from agents.trail import change_status, create_approval, save_offers
from agents.waitlist import offer_freed_slot
from api.db import get_connection

logger = logging.getLogger("careflow")


class BookingError(Exception):
    def __init__(self, code, message, new_offers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.new_offers = new_offers or []


def book_choice(request_id: int, choice: int) -> dict:
    with get_connection() as conn:
        request = conn.execute(
            "select request_type from careflow.patient_requests where id = %s",
            (request_id,),
        ).fetchone()
    if request is not None and request["request_type"] == "reschedule":
        return _book_reschedule_choice(request_id, choice)
    return _book_regular_choice(request_id, choice)


def _book_regular_choice(request_id: int, choice: int) -> dict:
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


def _book_reschedule_choice(request_id: int, choice: int) -> dict:
    failure_context = None
    result = None
    freed_slot_id = None

    with get_connection() as conn:
        req = conn.execute(
            """select id, status, request_type, patient_id, offered_slots,
                      reschedule_appointment_id, extracted_data
               from careflow.patient_requests
               where id = %s for update""",
            (request_id,),
        ).fetchone()
        if req is None:
            raise BookingError("not_found", "Request not found")
        offers = req["offered_slots"] or []
        if req["request_type"] != "reschedule" or req["status"] != "waiting_patient" or not offers:
            raise BookingError("not_waiting", "This request is not waiting for a reschedule choice")
        if not req["patient_id"]:
            raise BookingError("no_patient", "No patient record is linked to this request yet")
        if choice < 1 or choice > len(offers):
            raise BookingError(
                "invalid_choice",
                f"Please choose one of the offered options, 1 to {len(offers)}",
            )
        if req["reschedule_appointment_id"] is None:
            raise BookingError("not_waiting", "This request has no appointment to reschedule")

        old = conn.execute(
            """select a.id, a.status, a.slot_id, a.patient_id,
                      s.doctor_id, doc.department_id
               from careflow.appointments a
               join careflow.time_slots s on s.id = a.slot_id
               join careflow.doctors doc on doc.id = s.doctor_id
               where a.id = %s
               for update of a""",
            (req["reschedule_appointment_id"],),
        ).fetchone()
        if old is None or old["status"] != "booked" or old["patient_id"] != req["patient_id"]:
            raise BookingError("not_waiting", "The original appointment is no longer available to reschedule")

        slot = offers[choice - 1]
        if slot["slot_id"] == old["slot_id"]:
            raise BookingError("slot_unavailable", "Choose a different appointment slot")

        got = conn.execute(
            """update careflow.time_slots
               set status = 'booked', held_until = null
               where id = %s and start_time > now()
                 and (status = 'free' or (status = 'held' and held_until < now()))
               returning id""",
            (slot["slot_id"],),
        ).fetchone()

        if got is None:
            failure_context = {
                "department_id": old["department_id"],
                "doctor_id": old["doctor_id"],
                "old_slot_id": old["slot_id"],
                "preferred": (req["extracted_data"] or {}).get("preferred_date_time"),
            }
        else:
            new_appointment = conn.execute(
                """insert into careflow.appointments (patient_id, slot_id, request_id)
                   values (%s, %s, %s) returning id, appointment_code""",
                (req["patient_id"], slot["slot_id"], request_id),
            ).fetchone()
            cancelled = conn.execute(
                """update careflow.appointments
                   set status = 'cancelled', updated_at = now()
                   where id = %s and status = 'booked'
                   returning slot_id""",
                (old["id"],),
            ).fetchone()
            if cancelled is None:
                raise BookingError("not_waiting", "The original appointment is no longer booked")
            freed = conn.execute(
                """update careflow.time_slots
                   set status = 'free', held_until = null
                   where id = %s and status = 'booked'
                   returning id""",
                (cancelled["slot_id"],),
            ).fetchone()
            if freed is None:
                raise BookingError("not_waiting", "The original appointment slot is no longer booked")

            updated = conn.execute(
                """update careflow.patient_requests
                   set status = 'executing', updated_at = now()
                   where id = %s and status = 'waiting_patient'
                   returning id""",
                (request_id,),
            ).fetchone()
            if updated is None:
                raise BookingError("not_waiting", "This request is no longer waiting for a choice")
            conn.execute(
                """insert into careflow.request_events
                     (request_id, from_status, to_status, actor, note)
                   values (%s, 'waiting_patient', 'executing', 'system', %s)""",
                (request_id, f"Patient chose reschedule option {choice}"),
            )
            result = {
                "status": "rescheduled",
                "message": "Your appointment has been rescheduled.",
                "appointment_id": new_appointment["id"],
                "appointment_code": new_appointment["appointment_code"],
                "appointment": slot["label"],
            }
            freed_slot_id = freed["id"]

    if failure_context is not None:
        fresh = find_reschedule_offers(
            failure_context["department_id"],
            failure_context["preferred"],
            failure_context["old_slot_id"],
            failure_context["doctor_id"],
        )["offers"]
        if fresh:
            save_offers(request_id, fresh)
            raise BookingError(
                "slot_unavailable",
                "That slot is no longer available. Please choose one of these instead.",
                fresh,
            )

        create_approval(request_id, "other")
        change_status(
            request_id,
            "awaiting_approval",
            "system",
            "Chosen reschedule slot was taken and no other slots are free",
        )
        raise BookingError(
            "no_slots",
            "That slot is no longer available, and no other slots are free. Our staff will contact you.",
        )

    if result is None or freed_slot_id is None:
        raise RuntimeError("Reschedule transaction completed without a freed original slot")
    try:
        offer_freed_slot(freed_slot_id)
    except Exception as exc:
        logger.error("Waitlist offer after reschedule failed: %s", type(exc).__name__)
    return result