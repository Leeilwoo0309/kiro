# Requirements Document

## Introduction

AI 가계부는 사용자가 은행 또는 카드사에서 내려받은 CSV 거래 내역을 업로드하면, AI가 소비 패턴을 자동으로 분류·분석하고 "국정감사" 스타일의 날카로운 피드백과 저축 목표 달성을 위한 예산 삭감 추천을 제공하는 stateless 웹 애플리케이션이다. 별도의 데이터베이스 없이 요청 단위로 처리되며, AWS Bedrock과 Lambda 등 클라우드 인프라를 활용해 구현한다.

## Glossary

- **App**: AI 가계부 웹 애플리케이션 전체 시스템
- **User**: 거래 내역 CSV를 업로드하고 분석 결과를 확인하는 최종 사용자
- **CSV_Parser**: 업로드된 CSV 파일을 파싱하여 거래 항목 목록으로 변환하는 컴포넌트
- **Transaction**: 날짜, 금액, 상호명, 카드/계좌 구분을 포함한 단일 거래 항목
- **Category_Classifier**: AI를 이용해 각 Transaction을 미리 정의된 카테고리로 분류하는 컴포넌트
- **Category**: 식비, 교통, 쇼핑, 카페, 의료, 구독/OTT, 기타 등 지출 분류 레이블
- **Spending_Analyzer**: 분류된 거래 항목을 집계하여 카테고리별 지출 순위와 통계를 산출하는 컴포넌트
- **AI_Auditor**: 소비 패턴을 분석하고 국정감사 스타일의 피드백 코멘트를 생성하는 AI 컴포넌트 (AWS Bedrock 기반)
- **Budget_Advisor**: 저축 목표 금액을 입력받아 삭감 가능한 지출 항목과 절감 금액을 추천하는 컴포넌트
- **Analysis_Result**: CSV 파싱, 분류, 지출 순위, AI 코멘트, 예산 추천을 포함한 최종 분석 응답 객체
- **PDF_Parser**: 업로드된 텍스트 기반 PDF 파일을 파싱하여 거래 항목 목록을 추출하고, 이를 내부 CSV 형식으로 변환하는 컴포넌트
- **Savings_Goal**: 사용자가 입력한 월 목표 저축 금액 (원화 기준 양의 정수)

## Requirements

### Requirement 1: CSV 업로드 및 파싱

**User Story:** As a User, I want to upload my bank or credit card CSV transaction file, so that the App can read my spending history without manual data entry.

#### Acceptance Criteria

1. THE App SHALL provide a file upload interface that accepts files with the `.csv` extension and the `.pdf` extension.
2. WHEN a User uploads a CSV file, THE CSV_Parser SHALL parse the file and extract a list of Transactions containing date, amount, merchant name, and payment type (card or account transfer).
3. IF the uploaded file is not a valid CSV format, THEN THE CSV_Parser SHALL return an error message indicating that the file must be a valid CSV format.
4. WHEN the uploaded CSV file exceeds 10 MB in size, THEN THE App SHALL reject the file and display an error message before parsing begins.
5. WHEN the uploaded CSV file contains zero data rows after the header, THEN THE CSV_Parser SHALL return an error message indicating that no transactions were found.
6. THE CSV_Parser SHALL support CSV exports from at least 5 major Korean banks and credit card companies, including Kookmin Bank, Shinhan Bank, Woori Bank, Hana Bank, and Samsung Card, and SHALL correctly decode files encoded in UTF-8 and EUC-KR.
7. IF a data row in the uploaded CSV file is missing one or more of the required fields (date, amount, merchant name, or payment type), THEN THE CSV_Parser SHALL skip that row and continue parsing the remaining rows, and SHALL report the count of skipped rows to the User upon completion.
8. IF the payment type field of a parsed row contains a value other than card or account transfer, THEN THE CSV_Parser SHALL skip that row and include it in the skipped row count reported to the User.

### Requirement 2: 카테고리 자동 분류

**User Story:** As a User, I want each transaction to be automatically categorized, so that I can understand how my spending is distributed across different areas of life.

#### Acceptance Criteria

1. WHEN a list of Transactions is parsed, THE Category_Classifier SHALL assign exactly one Category to each Transaction.
2. THE Category_Classifier SHALL classify each Transaction into one of the following Categories: 식비, 교통, 쇼핑, 카페, 의료, 구독/OTT, 기타.
3. IF the merchant name field of a Transaction does not match any of the Categories 식비, 교통, 쇼핑, 카페, 의료, or 구독/OTT, THEN THE Category_Classifier SHALL assign the Category 기타 to that Transaction.
4. THE Category_Classifier SHALL classify Transactions based on the merchant name field of each Transaction.
5. THE App SHALL complete category classification for up to 500 Transactions within 10 seconds.
6. IF the merchant name field of a Transaction is empty or null, THEN THE Category_Classifier SHALL assign the Category 기타 to that Transaction.

### Requirement 3: 지출 순위 분석

**User Story:** As a User, I want to see a ranked breakdown of my spending by category, so that I can immediately identify where most of my money is going.

#### Acceptance Criteria

1. WHEN all Transactions have been classified, THE Spending_Analyzer SHALL compute the total amount spent per Category by summing the absolute values of all Transaction amounts within that Category.
2. THE Spending_Analyzer SHALL produce a ranked list of Categories ordered from highest to lowest total spending, with ties broken by alphabetical order of Category name.
3. THE Spending_Analyzer SHALL include the percentage share of total spending for each Category in the ranked list, calculated as (Category total ÷ sum of all Category totals) × 100, rounded to two decimal places.
4. THE Spending_Analyzer SHALL include the individual Transaction count per Category in the ranked list as a non-negative integer.
5. WHEN the total number of Transactions is zero, THEN THE Spending_Analyzer SHALL return an empty ranked list rather than an error.
6. IF two or more Categories have identical total spending amounts, THEN THE Spending_Analyzer SHALL order those Categories alphabetically by Category name in ascending order.

### Requirement 4: AI 국정감사 코멘트

**User Story:** As a User, I want to receive sharp, direct AI-generated feedback on my spending habits, so that I am confronted with honest insights that motivate behavior change.

#### Acceptance Criteria

1. WHEN the Spending_Analyzer produces a ranked spending list, THE AI_Auditor SHALL generate a written feedback report in Korean.
2. THE AI_Auditor SHALL reference the Category name and exact amount for each of the top 3 Categories from the Analysis_Result in the feedback report.
3. THE AI_Auditor SHALL identify at least one spending Category whose share exceeds 40% of total spending and explicitly name that Category and its percentage share in the feedback report.
4. THE AI_Auditor SHALL generate feedback using interrogative or declarative sentences that directly address the user's spending behavior without hedging language such as "아마도", "~인 것 같습니다", or "~일 수도 있습니다".
5. IF the AI_Auditor fails to generate a response due to an API error, THEN THE App SHALL display an error message in Korean indicating that AI analysis is temporarily unavailable and prompt the user to retry.
6. WHEN the Spending_Analyzer delivers the Analysis_Result to the AI_Auditor, THE AI_Auditor SHALL complete feedback generation and return the report within 30 seconds.

### Requirement 5: 저축 목표 기반 예산 삭감 추천

**User Story:** As a User, I want to enter a monthly savings goal and receive specific spending cut recommendations, so that I have a concrete action plan to reach my target.

#### Acceptance Criteria

1. THE App SHALL provide an input field for the User to enter a Savings_Goal as a positive integer amount in Korean Won (원), between 1 and 999,999,999 원 inclusive.
2. WHEN a User submits a Savings_Goal, THE Budget_Advisor SHALL identify Categories where spending can be reduced to meet the Savings_Goal, prioritizing Categories with the highest total spending first.
3. WHEN a User submits a Savings_Goal, THE Budget_Advisor SHALL present each recommended cut as a specific Category name, the current total amount for that Category, a suggested reduced amount of at least 0 원, and the resulting savings amount.
4. THE Budget_Advisor SHALL ensure that the sum of all recommended reductions across all presented Categories is greater than or equal to the Savings_Goal.
5. WHEN the Savings_Goal exceeds the total reducible spending across all Categories, THEN THE Budget_Advisor SHALL inform the User that the target cannot be fully met through spending cuts alone and SHALL present all Categories with their maximum possible reductions.
6. WHEN a User enters a Savings_Goal that is zero, negative, or greater than 999,999,999 원, THEN THE App SHALL display a validation error message indicating the valid input range and SHALL NOT invoke the Budget_Advisor.
7. WHEN a User submits a Savings_Goal but no Transaction data has been uploaded in the current session, THEN THE App SHALL display an error message indicating that spending data is required before recommendations can be generated and SHALL NOT invoke the Budget_Advisor.

### Requirement 6: Stateless 처리 및 데이터 비저장

**User Story:** As a User, I want my transaction data to be processed without being permanently stored, so that my financial information remains private.

#### Acceptance Criteria

1. THE App SHALL process each CSV upload as an independent, stateless request without persisting Transaction data to any database, persistent storage, or server-side cache shared across requests.
2. WHEN an Analysis_Result is returned to the User, THE App SHALL ensure that all parsed Transaction data from that request is no longer accessible in any subsequent request or response.
3. THE App SHALL NOT associate any Transaction data with a user identity or session identifier beyond the duration of a single request-response cycle.
4. THE App SHALL transmit all Transaction data between the client and server over HTTPS.
5. IF a client attempts to submit Transaction data over a non-HTTPS connection, THEN THE App SHALL reject the request and SHALL return an error response indicating that a secure connection is required.

### Requirement 7: PDF 업로드 및 CSV 변환

**User Story:** As a User, I want to upload a PDF bank statement, so that I can use it for spending analysis without manually converting it.

#### Acceptance Criteria

1. THE App SHALL provide a file upload interface that accepts files with the `.pdf` extension.
2. WHEN a User uploads a PDF file, THE PDF_Parser SHALL extract a list of Transactions containing date, amount, merchant name, and payment type (one of: credit, debit, or transfer) from the file.
3. WHEN extraction is complete, THE PDF_Parser SHALL convert the extracted Transactions into the internal CSV format — preserving date, amount, merchant name, and payment type as columns in that order — and pass the result to the CSV_Parser pipeline for further processing.
4. THE App SHALL support text-based PDF files only; WHEN a User uploads an image-based scanned PDF file, THE App SHALL reject the file and display an informational message indicating that scanned PDFs are not supported.
5. IF the uploaded PDF file exceeds 20 MB in size, THEN THE App SHALL reject the file and display an error message before parsing begins.
6. IF the PDF_Parser fails to extract any Transactions from an uploaded PDF file, THEN THE App SHALL display an error message in Korean indicating that the file could not be parsed and prompt the User to verify the file format.
7. IF a row extracted from the PDF file is missing one or more of the required fields (date, amount, merchant name, or payment type), THEN THE PDF_Parser SHALL skip that row and THE App SHALL display the total count of skipped rows in the analysis result summary upon completion.
8. WHEN an Analysis_Result is returned to the User, THE App SHALL discard all data extracted from the original PDF file so that it is not included in any subsequent request or response.
