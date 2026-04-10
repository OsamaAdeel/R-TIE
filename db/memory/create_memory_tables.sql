CREATE TABLE rtie_conversations (
    id              SERIAL PRIMARY KEY,
    session_id      UUID NOT NULL,
    engineer_id     VARCHAR(100),
    query           TEXT,
    object_name     VARCHAR(200),
    object_type     VARCHAR(50),
    response        TEXT,
    confidence      FLOAT,
    validated       BOOLEAN,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_conversations_session
    ON rtie_conversations(session_id);
CREATE INDEX idx_conversations_object
    ON rtie_conversations(object_name);
