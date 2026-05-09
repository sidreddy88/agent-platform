# RDS Postgres 16 with pgvector. Lean sizing — db.t4g.micro single-AZ
# 20GB gp3, ~$13/mo.
#
# Why publicly_accessible=false even though we're in a public subnet:
#   - The subnet has a route to the IGW for everyone else, but RDS
#     advertises a *private* IP only when this flag is false. Combined
#     with the `rds` security group restricting ingress to the ECS task
#     SG, the database is unreachable from the internet at every layer.

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db-subnets"
  subnet_ids = aws_subnet.public[*].id

  tags = { Name = "${local.name}-db-subnets" }
}

# Postgres parameter group — pgvector extension is installed at app
# startup via the existing app.services.database.init_db() path, so no
# DB-level params are needed yet. Keeping the group around for future
# tuning (e.g. shared_buffers, work_mem) without recreating the instance.
resource "aws_db_parameter_group" "main" {
  name        = "${local.name}-pg16"
  family      = "postgres16"
  description = "Agent platform Postgres 16 + pgvector"
}

resource "aws_db_instance" "main" {
  identifier              = local.name
  engine                  = "postgres"
  engine_version          = "16.4"
  instance_class          = var.db_instance_class
  allocated_storage       = var.db_allocated_storage_gb
  storage_type            = "gp3"
  storage_encrypted       = true

  db_name                 = var.db_name
  username                = var.db_username
  password                = var.db_password

  parameter_group_name    = aws_db_parameter_group.main.name
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]

  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 7
  backup_window           = "08:00-09:00"   # UTC, off-peak
  maintenance_window      = "sun:09:00-sun:10:00"
  copy_tags_to_snapshot   = true

  deletion_protection     = true
  skip_final_snapshot     = false
  final_snapshot_identifier = "${local.name}-final-${formatdate("YYYY-MM-DD-hhmm", timestamp())}"

  apply_immediately = false

  lifecycle {
    # `final_snapshot_identifier` uses timestamp() which changes every
    # apply — ignore so we don't see a phantom diff.
    ignore_changes = [final_snapshot_identifier]
  }
}
