ALTER TABLE careflow.patient_requests
    ADD COLUMN IF NOT EXISTS department_id bigint
    REFERENCES careflow.departments(id);

INSERT INTO careflow.system_settings (key, value, description)
VALUES (
    'waitlist_expire_days',
    '30'::jsonb,
    'Number of days a waiting waitlist entry remains active'
)
ON CONFLICT (key) DO NOTHING;
