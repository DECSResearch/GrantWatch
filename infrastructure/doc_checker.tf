terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

variable "environment" {
  description = "Short environment suffix (e.g. dev, staging, prod)."
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "lambda_package_path" {
  description = "Path to the zipped Lambda artifact for ValidateDoc."
  type        = string
  default     = "../aws/dist/validate_doc.zip"
}

variable "allowed_cors_origins" {
  description = "Origins allowed to upload using presigned URLs."
  type        = list(string)
  default     = ["*"]
}

locals {
  bucket_name  = "grant-doc-checker-temp-${var.environment}"
  table_name   = "grant-doc-checker-submissions-${var.environment}"
  lambda_name  = "doc-checker-validate-${var.environment}"
}

resource "aws_s3_bucket" "grant_doc_checker" {
  bucket        = local.bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "grant_doc_checker" {
  bucket                  = aws_s3_bucket.grant_doc_checker.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "grant_doc_checker" {
  bucket = aws_s3_bucket.grant_doc_checker.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "grant_doc_checker" {
  bucket = aws_s3_bucket.grant_doc_checker.id

  rule {
    id     = "expire-temp-objects"
    status = "Enabled"

    expiration {
      days = 2
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "grant_doc_checker" {
  bucket = aws_s3_bucket.grant_doc_checker.id

  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = var.allowed_cors_origins
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

resource "aws_dynamodb_table" "submissions" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "submission_id"

  attribute {
    name = "submission_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_iam_role" "backend_role" {
  name = "doc-checker-backend-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role" "lambda_role" {
  name = "${local.lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "backend_policy" {
  role = aws_iam_role.backend_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.grant_doc_checker.arn,
          "${aws_s3_bucket.grant_doc_checker.arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource = aws_dynamodb_table.submissions.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:HeadObject"]
        Resource = "${aws_s3_bucket.grant_doc_checker.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:DescribeTable"]
        Resource = aws_dynamodb_table.submissions.arn
      },
      {
        Effect   = "Allow"
        Action   = ["textract:DetectDocumentText"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      }
    ]
  })
}

resource "aws_lambda_function" "validate_doc" {
  function_name = local.lambda_name
  role          = aws_iam_role.lambda_role.arn
  handler       = "validate_doc.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 512

  filename = var.lambda_package_path

  environment {
    variables = {
      DOC_CHECKER_TABLE           = aws_dynamodb_table.submissions.name
      DOC_CHECKER_BUCKET          = aws_s3_bucket.grant_doc_checker.bucket
      DOC_CHECKER_ENABLE_TEXTRACT = "false"
    }
  }
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validate_doc.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.doc_checker_object_created.arn
}

resource "aws_cloudwatch_event_rule" "doc_checker_object_created" {
  name = "doc-checker-object-created-${var.environment}"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [aws_s3_bucket.grant_doc_checker.bucket]
      },
      object = {
        key = [
          {
            prefix = "submissions/"
          }
        ]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "doc_checker_lambda" {
  rule      = aws_cloudwatch_event_rule.doc_checker_object_created.name
  target_id = "doc-checker-validate"
  arn       = aws_lambda_function.validate_doc.arn
}

output "bucket_name" {
  value       = aws_s3_bucket.grant_doc_checker.bucket
  description = "Temporary document checker bucket"
}

output "dynamodb_table" {
  value       = aws_dynamodb_table.submissions.name
  description = "DynamoDB table for submission tracking"
}

output "lambda_function" {
  value       = aws_lambda_function.validate_doc.function_name
  description = "Validator Lambda name"
}
