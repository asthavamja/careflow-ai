ALTER TABLE careflow.patient_requests
    ADD COLUMN IF NOT EXISTS reschedule_appointment_id bigint
    REFERENCES careflow.appointments(id);
