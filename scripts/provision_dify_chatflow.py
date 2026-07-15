import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import flask_app


APP_NAME = "星河优选 AI 客服"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsl", required=True)
    parser.add_argument("--secret-output", required=True)
    args = parser.parse_args()

    dsl_content = Path(args.dsl).read_text(encoding="utf-8")
    if "__DIFY_DATASET_ID__" in dsl_content:
        raise SystemExit("The rendered DSL still contains a dataset placeholder.")

    with flask_app.app_context():
        from extensions.ext_database import db
        from models.account import Account, TenantAccountJoin
        from models.enums import ApiTokenType
        from models.model import ApiToken, App
        from libs.datetime_utils import naive_utc_now
        from services.app_dsl_service import AppDslService, ImportStatus
        from services.workflow_service import WorkflowService

        with Session(db.engine, expire_on_commit=False) as session:
            account_row = session.execute(
                select(Account, TenantAccountJoin)
                .join(
                    TenantAccountJoin,
                    TenantAccountJoin.account_id == Account.id,
                )
                .order_by(Account.created_at.asc())
                .limit(1)
            ).first()
            if account_row is None:
                raise SystemExit("Dify has no initialized account and tenant.")

            account, tenant_join = account_row
            account.set_tenant_id(tenant_join.tenant_id)
            existing_app = session.scalar(
                select(App).where(
                    App.tenant_id == tenant_join.tenant_id,
                    App.name == APP_NAME,
                )
            )

            import_result = AppDslService(session).import_app(
                account=account,
                import_mode="yaml-content",
                yaml_content=dsl_content,
                app_id=existing_app.id if existing_app else None,
            )
            if import_result.status not in {
                ImportStatus.COMPLETED,
                ImportStatus.COMPLETED_WITH_WARNINGS,
            }:
                session.rollback()
                raise SystemExit(f"Dify DSL import failed: {import_result.error}")
            session.commit()

            app_model = session.scalar(
                select(App).where(App.id == import_result.app_id)
            )
            if app_model is None:
                raise SystemExit("Imported Dify app was not found.")

            published_workflow = WorkflowService().publish_workflow(
                session=session,
                app_model=app_model,
                account=account,
                marked_name="Initial AI customer service release",
            )
            app_model.workflow_id = published_workflow.id
            app_model.updated_by = account.id
            app_model.updated_at = naive_utc_now()

            api_token = session.scalar(
                select(ApiToken)
                .where(
                    ApiToken.app_id == app_model.id,
                    ApiToken.type == ApiTokenType.APP,
                )
                .order_by(ApiToken.created_at.asc())
                .limit(1)
            )
            if api_token is None:
                api_token = ApiToken()
                api_token.app_id = app_model.id
                api_token.tenant_id = app_model.tenant_id
                api_token.type = ApiTokenType.APP
                api_token.token = ApiToken.generate_api_key("app-", 24)
                session.add(api_token)
            session.commit()

            secret_path = Path(args.secret_output)
            secret_path.write_text(api_token.token, encoding="utf-8")
            os.chmod(secret_path, 0o600)
            print(
                json.dumps(
                    {
                        "app_id": app_model.id,
                        "status": "published",
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()
