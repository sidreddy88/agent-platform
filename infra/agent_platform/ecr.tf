# Container image registry. CI/CD (PR 6) pushes to this repo, then
# updates the ECS service to roll out the new image tag.

resource "aws_ecr_repository" "agent_platform" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keep the registry from accumulating decades of images.
resource "aws_ecr_lifecycle_policy" "agent_platform" {
  repository = aws_ecr_repository.agent_platform.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}
