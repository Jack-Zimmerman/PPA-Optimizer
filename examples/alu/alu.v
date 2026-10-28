// 8-bit registered ALU: add, sub, and, or, xor, shifts, less-than.
module alu (
    input            clk,
    input            rst,
    input      [7:0] a,
    input      [7:0] b,
    input      [2:0] op,
    output reg [7:0] y,
    output reg       lt
);
    reg [7:0] y_next;
    always @(*) begin
        case (op)
            3'd0: y_next = a + b;
            3'd1: y_next = a - b;
            3'd2: y_next = a & b;
            3'd3: y_next = a | b;
            3'd4: y_next = a ^ b;
            3'd5: y_next = a << b[2:0];
            3'd6: y_next = a >> b[2:0];
            default: y_next = 8'd0;
        endcase
    end
    always @(posedge clk) begin
        if (rst) begin y <= 8'd0; lt <= 1'b0; end
        else     begin y <= y_next; lt <= (a < b); end
    end
endmodule
