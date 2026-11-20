"""仓表结构。

按“证据 + 期间 + 事实”建模，不按套号聚合销量：

- 前区（landing_*）只追加，保存每次接收的原始行、文件摘要与接收次序；
- 金区（current_*）是各记录键的最新接收版本；
- 会话事实（fact_*、rule_period）按会话物化，旧会话不被后续修订改写；
- 集市（mart_*）按查询时点生成，不写回事实。

时间区间统一左闭右开 [effective, next)，空表示仍在生效。
业务时刻在仓内保存为业务本地时间 TIMESTAMP；接收时刻另存 decided_at，二者不混用。
"""

DDL = [
    # ---------------------------------------------------------------- 会话与接收
    """
    CREATE TABLE meta_session (
        session_id      VARCHAR PRIMARY KEY,
        created_at      TIMESTAMP NOT NULL,
        label           VARCHAR NOT NULL,
        catalog_sha256  VARCHAR NOT NULL,
        previous_session VARCHAR,
        sealed_at       TIMESTAMP
    )
    """,
    """
    CREATE TABLE meta_source_pack (
        session_id  VARCHAR NOT NULL,
        table_name  VARCHAR NOT NULL,
        path        VARCHAR NOT NULL,
        sha256      VARCHAR NOT NULL,
        row_count   INTEGER NOT NULL,
        PRIMARY KEY (session_id, table_name)
    )
    """,
    """
    CREATE TABLE meta_receive (
        receive_id  BIGINT PRIMARY KEY,
        session_id  VARCHAR NOT NULL,
        table_name  VARCHAR NOT NULL,
        source_path VARCHAR NOT NULL,
        sha256      VARCHAR NOT NULL,
        row_count   INTEGER NOT NULL,
        decided_at  TIMESTAMP NOT NULL
    )
    """,
    # ---------------------------------------------------------------- 前区
    """
    CREATE TABLE landing_component (
        component_key VARCHAR, manufacturer VARCHAR, serial VARCHAR,
        class VARCHAR, lot VARCHAR,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_kit (
        kit_key VARCHAR, model VARCHAR, version VARCHAR,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_composition_rule (
        rule_id VARCHAR, model VARCHAR, version VARCHAR, class VARCHAR,
        required BOOLEAN, quantity INTEGER, choice_group VARCHAR,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_assembly (
        assembly_id VARCHAR, kit_key VARCHAR, component_key VARCHAR,
        start TIMESTAMP, "end" TIMESTAMP, basis VARCHAR,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_shipment (
        shipment_id VARCHAR, kit_key VARCHAR, customer VARCHAR,
        kind VARCHAR, "at" TIMESTAMP,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_scan (
        scan_id VARCHAR, component_key VARCHAR, shipment_id VARCHAR,
        from_location VARCHAR, to_location VARCHAR, "at" TIMESTAMP,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE landing_quality_event (
        event_id VARCHAR, component_key VARCHAR, state VARCHAR, "at" TIMESTAMP,
        source_path VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL
    )
    """,
    # ---------------------------------------------------------------- 金区：最新接收版本
    """
    CREATE TABLE current_component (
        component_key VARCHAR PRIMARY KEY, manufacturer VARCHAR, serial VARCHAR,
        class VARCHAR, lot VARCHAR, receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_kit (
        kit_key VARCHAR PRIMARY KEY, model VARCHAR, version VARCHAR,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_composition_rule (
        rule_id VARCHAR PRIMARY KEY, model VARCHAR, version VARCHAR, class VARCHAR,
        required BOOLEAN, quantity INTEGER, choice_group VARCHAR,
        receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_assembly (
        assembly_id VARCHAR PRIMARY KEY, kit_key VARCHAR, component_key VARCHAR,
        start TIMESTAMP, "end" TIMESTAMP, basis VARCHAR, receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_shipment (
        shipment_id VARCHAR PRIMARY KEY, kit_key VARCHAR, customer VARCHAR,
        kind VARCHAR, "at" TIMESTAMP, receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_quality_event (
        event_id VARCHAR PRIMARY KEY, component_key VARCHAR, state VARCHAR,
        "at" TIMESTAMP, receive_id BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE current_scan (
        scan_id VARCHAR PRIMARY KEY, component_key VARCHAR, shipment_id VARCHAR,
        from_location VARCHAR, to_location VARCHAR, "at" TIMESTAMP,
        receive_id BIGINT NOT NULL
    )
    """,
    # 组成规则的版本登记：同一 rule_id 每次修订一行，用于判断“首次出现”。
    """
    CREATE TABLE version_composition_rule (
        version_row_id BIGINT PRIMARY KEY,
        receive_id BIGINT NOT NULL,
        rule_id VARCHAR, model VARCHAR, version VARCHAR, class VARCHAR,
        required BOOLEAN, quantity INTEGER, choice_group VARCHAR
    )
    """,
    # 组成规则的会话快照 + 生效范围。每个会话物化自己“当时已知”的完整版本史，
    # 因此旧会话查询不会看到后来接收的组成修订。
    """
    CREATE TABLE rule_period (
        session_id VARCHAR NOT NULL,
        rule_id VARCHAR NOT NULL,
        receive_id BIGINT NOT NULL,
        effective_from TIMESTAMP NOT NULL,
        effective_to TIMESTAMP,
        model VARCHAR, version VARCHAR, class VARCHAR,
        required BOOLEAN, quantity INTEGER, choice_group VARCHAR,
        PRIMARY KEY (session_id, rule_id, effective_from)
    )
    """,
    # ---------------------------------------------------------------- 会话事实
    """
    CREATE TABLE fact_delivery (
        session_id  VARCHAR NOT NULL,
        shipment_id VARCHAR NOT NULL,
        kit_key     VARCHAR NOT NULL,
        customer    VARCHAR NOT NULL,
        kind        VARCHAR NOT NULL,     -- sale / loan
        "at"        TIMESTAMP NOT NULL,
        receive_id  BIGINT NOT NULL,
        evidence    JSON NOT NULL,
        PRIMARY KEY (session_id, shipment_id)
    )
    """,
    """
    CREATE TABLE fact_delivery_scan (
        session_id  VARCHAR NOT NULL,
        shipment_id VARCHAR NOT NULL,
        scan_id     VARCHAR NOT NULL,
        component_key VARCHAR NOT NULL,
        scan_at     TIMESTAMP NOT NULL,
        PRIMARY KEY (session_id, shipment_id, scan_id)
    )
    """,
    """
    CREATE TABLE fact_movement (
        session_id  VARCHAR NOT NULL,
        scan_id     VARCHAR NOT NULL,
        component_key VARCHAR NOT NULL,
        shipment_id VARCHAR,
        from_location VARCHAR,
        to_location VARCHAR,
        "at"        TIMESTAMP NOT NULL,
        receive_id  BIGINT NOT NULL,
        PRIMARY KEY (session_id, scan_id)
    )
    """,
    """
    CREATE TABLE fact_assembly (
        session_id  VARCHAR NOT NULL,
        assembly_id VARCHAR NOT NULL,
        kit_key     VARCHAR NOT NULL,
        component_key VARCHAR NOT NULL,
        start       TIMESTAMP NOT NULL,
        "end"       TIMESTAMP,
        basis       VARCHAR NOT NULL,
        receive_id  BIGINT NOT NULL,
        PRIMARY KEY (session_id, assembly_id)
    )
    """,
    """
    CREATE TABLE fact_quality_event (
        session_id  VARCHAR NOT NULL,
        event_id    VARCHAR NOT NULL,
        component_key VARCHAR NOT NULL,
        state       VARCHAR NOT NULL,
        "at"        TIMESTAMP NOT NULL,
        receive_id  BIGINT NOT NULL,
        PRIMARY KEY (session_id, event_id)
    )
    """,
    # ---------------------------------------------------------------- 集市
    """
    CREATE TABLE mart_contract_revision (
        revision_id BIGINT PRIMARY KEY,
        session_id  VARCHAR NOT NULL,
        created_at  TIMESTAMP NOT NULL,
        kind        VARCHAR NOT NULL,     -- reconcile / service / trace
        subject     VARCHAR NOT NULL,
        query_at    TIMESTAMP NOT NULL,
        as_of_session VARCHAR,
        status      VARCHAR NOT NULL,     -- confirmed / superseded
        body        JSON NOT NULL,
        supersedes  BIGINT,
        note        VARCHAR
    )
    """,
    """
    CREATE TABLE mart_current_availability (
        session_id VARCHAR NOT NULL,
        query_at   TIMESTAMP NOT NULL,
        customer   VARCHAR NOT NULL,
        kit_key    VARCHAR NOT NULL,
        model      VARCHAR,
        version    VARCHAR,
        delivered_sets INTEGER NOT NULL,
        available_sets INTEGER NOT NULL,
        service_interrupted BOOLEAN NOT NULL,
        detail     JSON NOT NULL,
        PRIMARY KEY (session_id, customer, kit_key)
    )
    """,
    """
    CREATE TABLE mart_position (
        session_id VARCHAR NOT NULL,
        item_key   VARCHAR NOT NULL,
        location   VARCHAR,
        quality_state VARCHAR,
        quality_at TIMESTAMP,
        PRIMARY KEY (session_id, item_key)
    )
    """,
    """
    CREATE TABLE mart_conflict (
        session_id VARCHAR NOT NULL,
        kind       VARCHAR NOT NULL,
        severity   VARCHAR NOT NULL,      -- review / info
        subject    VARCHAR NOT NULL,
        evidence   JSON NOT NULL,
        PRIMARY KEY (session_id, kind, subject)
    )
    """,
]
