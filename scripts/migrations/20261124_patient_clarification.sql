BEGIN;

ALTER TABLE careflow.patient_requests
    ADD COLUMN IF NOT EXISTS contact_email text;

ALTER TABLE careflow.patient_requests
    ADD COLUMN IF NOT EXISTS clarification_count smallint NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'careflow.patient_requests'::regclass
          AND conname = 'patient_requests_clarification_count_check'
    ) THEN
        ALTER TABLE careflow.patient_requests
            ADD CONSTRAINT patient_requests_clarification_count_check
            CHECK (clarification_count BETWEEN 0 AND 1);
    END IF;
END;
$$;

ALTER TABLE careflow.notifications
    DROP CONSTRAINT IF EXISTS notifications_recipient_check;

ALTER TABLE careflow.notifications
    DROP CONSTRAINT IF EXISTS notifications_check;

ALTER TABLE careflow.notifications
    ADD CONSTRAINT notifications_recipient_check
    CHECK (
        (recipient_patient_id IS NOT NULL AND recipient_user_id IS NULL)
        OR (recipient_patient_id IS NULL AND recipient_user_id IS NOT NULL)
        OR (
            recipient_patient_id IS NULL
            AND recipient_user_id IS NULL
            AND request_id IS NOT NULL
            AND COALESCE(channel = 'email', false)
            AND COALESCE(
                template IN (
                    'registration_documents_request_v1',
                    'registration_documents_reminder_v1',
                    'patient_clarification_v1'
                ),
                false
            )
        )
    );

COMMIT;
